//! deepmem-capture: records what the user is looking at into SQLite.
//!
//! - `events`: one row per focus change (app, window title).
//! - `frames`: text of the focused window, read from the accessibility tree.
//!
//! Contract with the Python side: this daemon only writes these raw tables in
//! ~/.deepmem/db.sqlite (override with DEEPMEM_DB).

use accessibility_sys::{
    kAXErrorSuccess, AXError, AXIsProcessTrusted, AXUIElementCopyAttributeValue,
    AXUIElementCopyMultipleAttributeValues, AXUIElementCreateApplication,
    AXUIElementCreateSystemWide, AXUIElementRef, AXUIElementSetAttributeValue,
    AXUIElementSetMessagingTimeout,
};
use core_foundation::array::{CFArray, CFArrayRef};
use core_foundation::base::{CFType, CFTypeRef, TCFType};
use core_foundation::boolean::CFBoolean;
use core_foundation::runloop::{kCFRunLoopDefaultMode, CFRunLoop};
use core_foundation::string::CFString;
use core_foundation::url::CFURL;
use objc2_app_kit::NSWorkspace;
use rusqlite::Connection;
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use std::{env, fs, ptr};

const TICK: Duration = Duration::from_millis(250);
/// Input must be quiet this long before a capture (typing pause, scroll stop).
const SETTLE_SECS: f64 = 0.7;
/// Capture at least this often while the user is around.
const FALLBACK: Duration = Duration::from_secs(5);
/// No fallback captures after this much inactivity (user is away).
const AWAY_SECS: f64 = 60.0;

// ponytail: fixed caps keep one huge window from stalling capture; raise or
// make configurable if real documents get truncated.
const MAX_NODES: usize = 5_000;
const MAX_DEPTH: usize = 40;
const MAX_TEXT_BYTES: usize = 50_000;
const WALK_BUDGET: Duration = Duration::from_millis(1_500);

#[link(name = "CoreGraphics", kind = "framework")]
extern "C" {
    fn CGEventSourceSecondsSinceLastEventType(state: i32, event_type: u32) -> f64;
}

/// Seconds since the last keyboard or mouse input. Needs no permission.
fn idle_secs() -> f64 {
    // 0 = kCGEventSourceStateCombinedSessionState, !0 = kCGAnyInputEventType
    unsafe { CGEventSourceSecondsSinceLastEventType(0, !0) }
}

fn as_element(value: &CFType) -> AXUIElementRef {
    value.as_CFTypeRef() as AXUIElementRef
}

/// Copy one accessibility attribute. The returned CFType releases it on drop.
unsafe fn attr(element: AXUIElementRef, name: &str) -> Result<CFType, AXError> {
    let mut value: CFTypeRef = ptr::null();
    let name = CFString::new(name);
    let err = AXUIElementCopyAttributeValue(element, name.as_concrete_TypeRef(), &mut value);
    if err == kAXErrorSuccess && !value.is_null() {
        Ok(CFType::wrap_under_create_rule(value))
    } else {
        Err(err)
    }
}

fn app_element(pid: i32) -> CFType {
    unsafe { CFType::wrap_under_create_rule(AXUIElementCreateApplication(pid) as CFTypeRef) }
}

/// Ask an app to build its accessibility tree. Chromium and Electron apps
/// expose no web content until one of these is set. Errors are ignored: most
/// native apps do not know the attributes and do not need them.
/// Known side effect: AXEnhancedUserInterface can slow window animations in
/// some apps when third-party window managers move them.
fn enable_accessibility(app: &CFType) {
    for name in ["AXManualAccessibility", "AXEnhancedUserInterface"] {
        unsafe {
            AXUIElementSetAttributeValue(
                as_element(app),
                CFString::new(name).as_concrete_TypeRef(),
                CFBoolean::true_value().as_CFTypeRef(),
            );
        }
    }
}

fn focused_window(app: &CFType) -> Result<CFType, AXError> {
    unsafe { attr(as_element(app), "AXFocusedWindow") }
}

fn window_title(window: &CFType) -> String {
    unsafe { attr(as_element(window), "AXTitle") }
        .ok()
        .and_then(|t| t.downcast::<CFString>())
        .map(|s| s.to_string())
        .unwrap_or_default()
}

/// (app name, pid) of the frontmost app. Needs no permission, but only stays
/// current while the main run loop is pumped (see the loop in `main`).
fn frontmost() -> Option<(String, i32)> {
    let app = NSWorkspace::sharedWorkspace().frontmostApplication()?;
    let name = app.localizedName().map(|n| n.to_string()).unwrap_or_default();
    Some((name, app.processIdentifier()))
}

/// Collects the visible text of one window from its accessibility tree.
struct Walker {
    attrs: CFArray<CFString>,
    text: String,
    url: Option<String>,
    nodes: usize,
    deadline: Instant,
    last_line: String,
}

impl Walker {
    // Order matters: `visit` reads the reply by index.
    const ATTRS: [&'static str; 7] =
        ["AXRole", "AXSubrole", "AXValue", "AXTitle", "AXDescription", "AXURL", "AXChildren"];

    fn new() -> Self {
        let names: Vec<CFString> = Self::ATTRS.iter().map(|a| CFString::new(a)).collect();
        Walker {
            attrs: CFArray::from_CFTypes(&names),
            text: String::new(),
            url: None,
            nodes: 0,
            deadline: Instant::now() + WALK_BUDGET,
            last_line: String::new(),
        }
    }

    fn push(&mut self, line: &str) {
        let line = line.trim();
        // Title, value and description of one element often repeat each other.
        if line.is_empty() || line == self.last_line {
            return;
        }
        self.text.push_str(line);
        self.text.push('\n');
        self.last_line = line.to_string();
    }

    fn full(&self) -> bool {
        self.nodes >= MAX_NODES
            || self.text.len() >= MAX_TEXT_BYTES
            || Instant::now() >= self.deadline
    }

    fn visit(&mut self, element: AXUIElementRef, depth: usize) {
        if depth > MAX_DEPTH || self.full() {
            return;
        }
        self.nodes += 1;

        // One round trip for all attributes. Missing ones come back as error
        // placeholders, which the downcasts below reject.
        let mut raw: CFArrayRef = ptr::null();
        let err = unsafe {
            AXUIElementCopyMultipleAttributeValues(
                element,
                self.attrs.as_concrete_TypeRef(),
                0,
                &mut raw,
            )
        };
        if err != kAXErrorSuccess || raw.is_null() {
            return;
        }
        let values: CFArray<CFType> = unsafe { CFArray::wrap_under_create_rule(raw) };
        let string = |i: isize| {
            values.get(i).and_then(|v| v.downcast::<CFString>()).map(|s| s.to_string())
        };

        // Never read password fields.
        if string(1).as_deref() == Some("AXSecureTextField") {
            return;
        }
        for i in [3, 2, 4] {
            if let Some(s) = string(i) {
                self.push(&s);
            }
        }
        if self.url.is_none() && string(0).as_deref() == Some("AXWebArea") {
            self.url = values
                .get(5)
                .and_then(|v| v.downcast::<CFURL>())
                .map(|u| u.get_string().to_string());
        }
        if let Some(children) = values.get(6).and_then(|v| v.downcast::<CFArray>()) {
            for child in children.iter() {
                self.visit(*child as AXUIElementRef, depth + 1);
            }
        }
    }
}

/// Decides whether a captured text is worth storing: not blank, and different
/// from the previously stored one.
#[derive(Default)]
struct Dedupe(Option<u64>);

impl Dedupe {
    fn is_new(&mut self, text: &str) -> bool {
        if text.trim().is_empty() {
            return false;
        }
        let mut hasher = DefaultHasher::new();
        text.hash(&mut hasher);
        let hash = Some(hasher.finish());
        if hash == self.0 {
            return false;
        }
        self.0 = hash;
        true
    }
}

fn now_ms() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as i64
}

fn db_path() -> PathBuf {
    env::var_os("DEEPMEM_DB").map(PathBuf::from).unwrap_or_else(|| {
        PathBuf::from(env::var_os("HOME").expect("HOME not set")).join(".deepmem/db.sqlite")
    })
}

fn open_db() -> rusqlite::Result<Connection> {
    let path = db_path();
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir).expect("cannot create database directory");
    }
    let db = Connection::open(&path)?;
    db.pragma_update(None, "journal_mode", "WAL")?;
    db.busy_timeout(Duration::from_secs(5))?; // the Python side writes its index here too
    db.execute_batch(
        "CREATE TABLE IF NOT EXISTS events (
            id    INTEGER PRIMARY KEY,
            ts    INTEGER NOT NULL, -- unix milliseconds
            app   TEXT NOT NULL,
            title TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS frames (
            id    INTEGER PRIMARY KEY,
            ts    INTEGER NOT NULL, -- unix milliseconds
            app   TEXT NOT NULL,
            title TEXT NOT NULL,
            url   TEXT,             -- set when the window shows web content
            text  TEXT NOT NULL
        );",
    )?;
    eprintln!("deepmem-capture: writing to {}", path.display());
    Ok(db)
}

fn main() -> rusqlite::Result<()> {
    if !unsafe { AXIsProcessTrusted() } {
        eprintln!(
            "deepmem-capture: no Accessibility permission, recording app names \
             only (no window titles, no screen text).\nSystem Settings > Privacy \
             & Security > Accessibility: enable the terminal (or app) that runs \
             this binary, then start it again."
        );
    }
    let db = open_db()?;
    let debug = env::var_os("DEEPMEM_DEBUG").is_some();

    // One slow or hung app must not stall the loop.
    unsafe { AXUIElementSetMessagingTimeout(AXUIElementCreateSystemWide(), 0.5) };

    let mut last_focus: Option<(String, String)> = None;
    let mut last_pid = 0;
    let mut last_error: AXError = kAXErrorSuccess;
    let mut last_capture = Instant::now() - FALLBACK;
    let mut dedupe = Dedupe::default();

    // ponytail: polls every 250 ms and infers "typing pause / scroll stop /
    // click" from the system idle timer, which needs no extra permission.
    // Switch to AXObserver + event tap only if polling cost shows up in
    // profiles; an event tap needs the Input Monitoring permission.
    loop {
        CFRunLoop::run_in_mode(unsafe { kCFRunLoopDefaultMode }, TICK, false);
        let Some((app, pid)) = frontmost() else { continue };
        let app_ax = app_element(pid);
        if pid != last_pid {
            enable_accessibility(&app_ax);
            last_pid = pid;
        }
        let window = focused_window(&app_ax);
        if let Err(err) = &window {
            if *err != last_error {
                eprintln!("deepmem-capture: no focused window for {app} (AXError {err})");
                last_error = *err;
            }
        }
        let title = window.as_ref().map(window_title).unwrap_or_default();

        let focus = Some((app, title));
        let focus_changed = focus != last_focus;
        let (app, title) = focus.as_ref().unwrap();
        if focus_changed {
            db.execute(
                "INSERT INTO events (ts, app, title) VALUES (?1, ?2, ?3)",
                (now_ms(), app, title),
            )?;
        }

        let idle = idle_secs();
        let since_capture = last_capture.elapsed();
        let input_settled = idle >= SETTLE_SECS && idle < since_capture.as_secs_f64();
        let fallback_due = since_capture >= FALLBACK && idle < AWAY_SECS;
        if let (Ok(window), true) = (&window, focus_changed || input_settled || fallback_due) {
            let mut walker = Walker::new();
            walker.visit(as_element(window), 0);
            last_capture = Instant::now();
            if dedupe.is_new(&walker.text) {
                db.execute(
                    "INSERT INTO frames (ts, app, title, url, text) VALUES (?1, ?2, ?3, ?4, ?5)",
                    (now_ms(), app, title, &walker.url, &walker.text),
                )?;
                if debug {
                    eprintln!(
                        "frame: {app}, {} nodes, {} bytes, url: {}",
                        walker.nodes,
                        walker.text.len(),
                        walker.url.is_some()
                    );
                }
            }
        }
        last_focus = focus;
    }
}

#[cfg(test)]
mod tests {
    use super::Dedupe;

    #[test]
    fn dedupe_skips_blank_and_repeated_text() {
        let mut d = Dedupe::default();
        assert!(!d.is_new(""));
        assert!(!d.is_new("  \n"));
        assert!(d.is_new("hello"));
        assert!(!d.is_new("hello"));
        assert!(d.is_new("hello world"));
        assert!(d.is_new("hello")); // only the previous frame counts
    }
}
