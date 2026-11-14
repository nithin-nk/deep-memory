//! deepmem-capture: records what the user is looking at into SQLite.
//!
//! - `events`: one row per focus change (app, window title).
//! - `frames`: text of the focused window, read from the accessibility tree.
//!
//! Contract with the Python side: this daemon is the only one that inserts
//! into these raw tables in ~/.deepmem/db.sqlite (override with DEEPMEM_DB).
//!
//! Secrets are redacted (see `redact.rs`) before any row is inserted.
//!
//! Privacy controls (all state lives in ~/.deepmem, override with DEEPMEM_HOME):
//! - `paused` flag file: while it exists nothing is recorded. Toggled by
//!   Ctrl+Option+Cmd+P, the menu bar item, or `deepmem pause` / `deepmem resume`.
//! - `config.toml`: `denylist = ["App"]` adds apps that are never recorded,
//!   `url_blocklist = ["site.com"]` websites. Changes apply without a restart.
//!
//! The menu bar item opens a window (see `window.rs`) for reading and
//! deleting memories and for changing these settings.

mod api;
mod config;
mod redact;
mod window;

use accessibility_sys::{
    kAXErrorSuccess, AXError, AXIsProcessTrusted, AXUIElementCopyAttributeValue,
    AXUIElementCopyMultipleAttributeValues, AXUIElementCreateApplication,
    AXUIElementCreateSystemWide, AXUIElementRef, AXUIElementSetAttributeValue,
    AXUIElementSetMessagingTimeout,
};
use core_foundation::array::{CFArray, CFArrayRef};
use core_foundation::base::{CFType, CFTypeRef, TCFType};
use core_foundation::boolean::CFBoolean;
use core_foundation::string::CFString;
use core_foundation::url::CFURL;
use objc2::rc::Retained;
use objc2::runtime::{AnyObject, NSObject};
use objc2::{define_class, msg_send, sel, MainThreadMarker, MainThreadOnly};
use objc2_app_kit::{
    NSApplication, NSApplicationActivationPolicy, NSEventMask, NSMenu, NSMenuItem, NSStatusBar,
    NSStatusItem, NSWorkspace,
};
use objc2_foundation::{NSDate, NSDefaultRunLoopMode, NSString};
use redact::redact;
use rusqlite::Connection;
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::ffi::c_void;
use std::path::{Path, PathBuf};
use std::process::{self, Command};
use std::sync::atomic::{AtomicBool, Ordering};
use window::Window;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use std::{env, fs, ptr, thread};

const TICK_SECS: f64 = 0.25;
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

// --- pause shortcut -------------------------------------------------------
// Carbon hot keys need no permission (an event tap would need Input
// Monitoring). They are delivered by the NSApplication event pump in `main`.

#[repr(C)]
struct EventHotKeyID {
    signature: u32,
    id: u32,
}

#[repr(C)]
struct EventTypeSpec {
    class: u32,
    kind: u32,
}

type EventHandler = extern "C" fn(*mut c_void, *mut c_void, *mut c_void) -> i32;

#[link(name = "Carbon", kind = "framework")]
extern "C" {
    fn GetApplicationEventTarget() -> *mut c_void;
    fn RegisterEventHotKey(
        key_code: u32,
        modifiers: u32,
        id: EventHotKeyID,
        target: *mut c_void,
        options: u32,
        out_ref: *mut *mut c_void,
    ) -> i32;
    fn InstallEventHandler(
        target: *mut c_void,
        handler: EventHandler,
        num_types: usize,
        types: *const EventTypeSpec,
        user_data: *mut c_void,
        out_ref: *mut *mut c_void,
    ) -> i32;
}

/// Set by the shortcut and by the menu bar item; the main loop flips the flag file.
static TOGGLE_REQUESTED: AtomicBool = AtomicBool::new(false);

/// Set by the menu bar item; the main loop creates or shows the window.
static OPEN_REQUESTED: AtomicBool = AtomicBool::new(false);

extern "C" fn on_hotkey(_: *mut c_void, _: *mut c_void, _: *mut c_void) -> i32 {
    TOGGLE_REQUESTED.store(true, Ordering::Relaxed);
    0
}

/// Registers Ctrl+Option+Cmd+P. Returns false if macOS refuses (for example
/// another app owns the combination); `deepmem pause` still works then.
// ponytail: fixed shortcut; read it from config.toml when someone needs another.
fn register_pause_hotkey() -> bool {
    const KEY_P: u32 = 35; // kVK_ANSI_P
    const CTRL_OPTION_CMD: u32 = 0x1000 | 0x0800 | 0x0100;
    let pressed = EventTypeSpec { class: u32::from_be_bytes(*b"keyb"), kind: 5 }; // kEventHotKeyPressed
    unsafe {
        let target = GetApplicationEventTarget();
        let mut unused = ptr::null_mut();
        InstallEventHandler(target, on_hotkey, 1, &pressed, ptr::null_mut(), &mut unused) == 0
            && RegisterEventHotKey(
                KEY_P,
                CTRL_OPTION_CMD,
                EventHotKeyID { signature: u32::from_be_bytes(*b"dmem"), id: 1 },
                target,
                0,
                &mut unused,
            ) == 0
    }
}

fn notify(message: &'static str) {
    eprintln!("deepmem-capture: {message}");
    // ponytail: notification through osascript; use UserNotifications when
    // the daemon gets an app bundle (Slice 10).
    thread::spawn(move || {
        let script = format!("display notification \"{message}\" with title \"deep-memory\"");
        let _ = Command::new("osascript").args(["-e", &script]).status();
    });
}

// --- menu bar item --------------------------------------------------------

define_class!(
    // Receives the menu clicks.
    #[unsafe(super(NSObject))]
    #[thread_kind = MainThreadOnly]
    #[name = "DeepmemMenuTarget"]
    struct MenuTarget;

    impl MenuTarget {
        #[unsafe(method(togglePause:))]
        fn toggle_pause(&self, _sender: Option<&AnyObject>) {
            TOGGLE_REQUESTED.store(true, Ordering::Relaxed);
        }

        #[unsafe(method(openWindow:))]
        fn open_window(&self, _sender: Option<&AnyObject>) {
            OPEN_REQUESTED.store(true, Ordering::Relaxed);
        }
    }
);

/// Shows capture state in the menu bar: "◉" recording, "⏸" paused.
struct MenuBar {
    mtm: MainThreadMarker,
    item: Retained<NSStatusItem>,
    state_line: Retained<NSMenuItem>,
    toggle: Retained<NSMenuItem>,
    _target: Retained<MenuTarget>, // menu items hold their target weakly
}

impl MenuBar {
    fn new(mtm: MainThreadMarker) -> Self {
        let menu_item = |title: &str, action| unsafe {
            NSMenuItem::initWithTitle_action_keyEquivalent(
                NSMenuItem::alloc(mtm),
                &NSString::from_str(title),
                action,
                &NSString::from_str(""),
            )
        };
        let target: Retained<MenuTarget> =
            unsafe { msg_send![super(MenuTarget::alloc(mtm).set_ivars(())), init] };
        let state_line = menu_item("", None); // no action: shown greyed out
        let toggle = menu_item("", Some(sel!(togglePause:)));
        unsafe { toggle.setTarget(Some(&target)) };
        let open = menu_item("Open deep-memory…", Some(sel!(openWindow:)));
        unsafe { open.setTarget(Some(&target)) };
        let quit = menu_item("Quit deep-memory capture", Some(sel!(terminate:)));

        let menu = NSMenu::new(mtm);
        menu.addItem(&state_line);
        menu.addItem(&toggle);
        menu.addItem(&open);
        menu.addItem(&NSMenuItem::separatorItem(mtm));
        menu.addItem(&quit);

        let item = NSStatusBar::systemStatusBar().statusItemWithLength(-1.0); // variable width
        item.setMenu(Some(&menu));
        let bar = MenuBar { mtm, item, state_line, toggle, _target: target };
        bar.set_paused(false);
        bar
    }

    fn set_paused(&self, paused: bool) {
        let (icon, state, action) = if paused {
            ("⏸", "deep-memory: paused", "Resume capture  (⌃⌥⌘P)")
        } else {
            ("◉", "deep-memory: recording", "Pause capture  (⌃⌥⌘P)")
        };
        if let Some(button) = self.item.button(self.mtm) {
            button.setTitle(&NSString::from_str(icon));
        }
        self.state_line.setTitle(&NSString::from_str(state));
        self.toggle.setTitle(&NSString::from_str(action));
    }
}

// --- what must never be recorded ------------------------------------------

/// Private browsing windows, recognised by the marker browsers put in the
/// window title, and the daemon's own terminal window (its log output would
/// otherwise be captured in a loop).
fn window_denied(title: &str) -> bool {
    const MARKERS: [&str; 6] = [
        "(Incognito)",          // Chrome, Chromium, Arc
        "Private Browsing",     // Firefox, Safari
        "InPrivate",            // Edge
        "(Private)",            // Brave, Vivaldi
        "Private Window",       // Opera, Brave Tor
        "deepmem-capture",      // this daemon's terminal
    ];
    MARKERS.iter().any(|m| title.contains(m))
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

fn home_dir() -> PathBuf {
    env::var_os("DEEPMEM_HOME").map(PathBuf::from).unwrap_or_else(|| {
        PathBuf::from(env::var_os("HOME").expect("HOME not set")).join(".deepmem")
    })
}

fn open_db(path: &Path) -> rusqlite::Result<Connection> {
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir).expect("cannot create database directory");
    }
    let db = Connection::open(path)?;
    db.pragma_update(None, "journal_mode", "WAL")?;
    db.pragma_update(None, "secure_delete", "ON")?; // deleted rows are overwritten, not just unlinked
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
    redact_existing_rows(&db)?;
    eprintln!("deepmem-capture: writing to {}", path.display());
    Ok(db)
}

/// One-time cleanup: rows written before the daemon redacted at insert time
/// may hold secrets. `user_version` 1 marks a database that is clean.
fn redact_existing_rows(db: &Connection) -> rusqlite::Result<()> {
    if db.query_row("PRAGMA user_version", [], |row| row.get::<_, i64>(0))? >= 1 {
        return Ok(());
    }
    let mut changed = 0;
    {
        let mut frames = db.prepare("SELECT id, title, url, text FROM frames")?;
        let rows = frames.query_map([], |r| {
            Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?, r.get::<_, Option<String>>(2)?, r.get::<_, String>(3)?))
        })?;
        for row in rows {
            let (id, title, url, text) = row?;
            let clean = (redact(&title), url.as_deref().map(redact), redact(&text));
            if clean != (title, url, text) {
                changed += db.execute(
                    "UPDATE frames SET title = ?1, url = ?2, text = ?3 WHERE id = ?4",
                    (&clean.0, &clean.1, &clean.2, id),
                )?;
            }
        }
        let mut events = db.prepare("SELECT id, title FROM events")?;
        let rows = events.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))?;
        for row in rows {
            let (id, title) = row?;
            let clean = redact(&title);
            if clean != title {
                changed += db.execute("UPDATE events SET title = ?1 WHERE id = ?2", (&clean, id))?;
            }
        }
    }
    db.pragma_update(None, "user_version", 1)?;
    // The WAL still holds the old pages until it is checkpointed.
    db.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |_| Ok(()))?;
    if changed > 0 {
        eprintln!("deepmem-capture: redacted secrets in {changed} existing rows");
    }
    Ok(())
}

/// Removes what was already stored for the focused window, once it turns
/// out to show a blocked site. Rows are overwritten (`secure_delete`), and
/// the WAL, which still holds the old pages, is truncated.
fn forget_focus(db: &Connection, event: Option<i64>, first_frame: Option<i64>) -> rusqlite::Result<()> {
    if event.is_none() && first_frame.is_none() {
        return Ok(());
    }
    if let Some(id) = event {
        db.execute("DELETE FROM events WHERE id = ?1", [id])?;
    }
    if let Some(id) = first_frame {
        // This daemon is the only inserter, so every later frame is from this focus.
        db.execute("DELETE FROM frames WHERE id >= ?1", [id])?;
    }
    db.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |_| Ok(()))
}

fn modified(path: &Path) -> Option<SystemTime> {
    fs::metadata(path).and_then(|meta| meta.modified()).ok()
}

fn main() -> rusqlite::Result<()> {
    let trusted = unsafe { AXIsProcessTrusted() };
    if !trusted {
        eprintln!(
            "deepmem-capture: no Accessibility permission, recording app names \
             only (no window titles, no screen text).\nSystem Settings > Privacy \
             & Security > Accessibility: enable the terminal (or app) that runs \
             this binary, then start it again."
        );
    }
    // Accessory app: menu bar item, no Dock icon.
    let mtm = MainThreadMarker::new().expect("must run on the main thread");
    let ns_app = NSApplication::sharedApplication(mtm);
    ns_app.setActivationPolicy(NSApplicationActivationPolicy::Accessory);
    ns_app.finishLaunching();
    let menu_bar = MenuBar::new(mtm);
    window::install_edit_menu(mtm, &ns_app);

    let home = home_dir();
    fs::create_dir_all(&home).expect("cannot create deep-memory directory");
    // A config file that cannot be parsed stops the daemon: privacy settings
    // must not be ignored silently.
    let config_file = home.join("config.toml");
    let mut config_modified = modified(&config_file);
    let mut rules = config::load_rules(&home).unwrap_or_else(|err| {
        eprintln!("deepmem-capture: {err}");
        process::exit(1);
    });
    let paused_flag = home.join("paused");
    let db_path =
        env::var_os("DEEPMEM_DB").map(PathBuf::from).unwrap_or_else(|| home.join("db.sqlite"));
    let db = open_db(&db_path)?;
    let debug = env::var_os("DEEPMEM_DEBUG").is_some();
    let own_pid = process::id() as i32;
    let mut window: Option<Window> = None;
    if env::args().any(|arg| arg == "--window") {
        OPEN_REQUESTED.store(true, Ordering::Relaxed);
    }

    if register_pause_hotkey() {
        eprintln!("deepmem-capture: Ctrl+Option+Cmd+P pauses and resumes capture");
    } else {
        eprintln!("deepmem-capture: pause shortcut unavailable, use `deepmem pause`");
    }

    // One slow or hung app must not stall the loop.
    unsafe { AXUIElementSetMessagingTimeout(AXUIElementCreateSystemWide(), 0.5) };

    let mut was_paused = false;
    let mut last_focus: Option<(String, String)> = None;
    let mut last_pid = 0;
    let mut last_error: AXError = kAXErrorSuccess;
    let mut last_capture = Instant::now() - FALLBACK;
    let mut dedupe = Dedupe::default();
    // About the focused window: is its focus event still to be written, which
    // rows it has produced so far, and was it last seen on a blocked site.
    let mut event_pending = false;
    let mut focus_event: Option<i64> = None;
    let mut focus_first_frame: Option<i64> = None;
    let mut focus_blocked = false;

    // ponytail: polls every 250 ms and infers "typing pause / scroll stop /
    // click" from the system idle timer, which needs no extra permission.
    // Switch to AXObserver + event tap only if polling cost shows up in
    // profiles; an event tap needs the Input Monitoring permission.
    loop {
        // Handle menu clicks, the window and the shortcut for one tick. This
        // also pumps the main run loop, which NSWorkspace needs to stay current.
        let tick_end = NSDate::dateWithTimeIntervalSinceNow(TICK_SECS);
        while let Some(event) = ns_app.nextEventMatchingMask_untilDate_inMode_dequeue(
            NSEventMask::Any,
            Some(&tick_end),
            unsafe { NSDefaultRunLoopMode },
            true,
        ) {
            ns_app.sendEvent(&event);
        }

        if OPEN_REQUESTED.swap(false, Ordering::Relaxed) {
            window
                .get_or_insert_with(|| {
                    let ctx = api::Context { home: home.clone(), db_path: db_path.clone(), trusted };
                    Window::new(mtm, ctx)
                })
                .show(&ns_app);
        }

        // Saved from the window or edited by hand: new rules apply at once.
        let now_modified = modified(&config_file);
        if now_modified != config_modified {
            config_modified = now_modified;
            match config::load_rules(&home) {
                Ok(new_rules) => rules = new_rules,
                Err(err) => {
                    eprintln!("deepmem-capture: {err}");
                    notify("config.toml has an error. Keeping the previous privacy rules.");
                }
            }
        }

        if TOGGLE_REQUESTED.swap(false, Ordering::Relaxed) {
            let result = if paused_flag.exists() {
                fs::remove_file(&paused_flag)
            } else {
                fs::write(&paused_flag, "")
            };
            if let Err(err) = result {
                eprintln!("deepmem-capture: cannot change {}: {err}", paused_flag.display());
            }
        }
        // The flag file is the single source of truth, so the shortcut, the
        // window and the `deepmem` CLI cannot disagree, and a pause survives
        // a restart.
        let paused = paused_flag.exists();
        if paused != was_paused {
            menu_bar.set_paused(paused);
            notify(if paused { "Capture paused" } else { "Capture resumed" });
            was_paused = paused;
        }
        if paused {
            last_focus = None; // record the focused window again on resume
            continue;
        }

        let Some((app, pid)) = frontmost() else { continue };
        // Never the daemon's own window: it shows the user's memories.
        if pid == own_pid || config::app_denied(&app, &rules.denylist) {
            last_focus = None;
            continue; // before any accessibility call into the app
        }
        let app_ax = app_element(pid);
        if pid != last_pid {
            enable_accessibility(&app_ax);
            last_pid = pid;
        }
        let window_ax = focused_window(&app_ax);
        if let Err(err) = &window_ax {
            if *err != last_error {
                eprintln!("deepmem-capture: no focused window for {app} (AXError {err})");
                last_error = *err;
            }
        }
        let title = window_ax.as_ref().map(window_title).unwrap_or_default();
        if window_denied(&title) {
            last_focus = None;
            continue;
        }

        let focus = Some((app, redact(&title)));
        let focus_changed = focus != last_focus;
        let (app, title) = focus.as_ref().unwrap();
        if focus_changed {
            event_pending = true;
            focus_event = None;
            focus_first_frame = None;
            focus_blocked = false;
        }

        let idle = idle_secs();
        let since_capture = last_capture.elapsed();
        let input_settled = idle >= SETTLE_SECS && idle < since_capture.as_secs_f64();
        let fallback_due = since_capture >= FALLBACK && idle < AWAY_SECS;
        let mut frame: Option<(String, Option<String>, usize)> = None;
        if let (Ok(window_ax), true) = (&window_ax, focus_changed || input_settled || fallback_due) {
            // The page's URL is known only after the walk, so nothing about
            // this window, not even its title, is written before it.
            let mut walker = Walker::new();
            walker.visit(as_element(window_ax), 0);
            last_capture = Instant::now();
            // A walk that finds no URL (cut short, tree not built yet) does
            // not change what is known about this window.
            if let Some(url) = &walker.url {
                focus_blocked = config::url_blocked(url, &rules.url_blocklist);
            }
            frame = Some((redact(&walker.text), walker.url.as_deref().map(redact), walker.nodes));
        }
        if focus_blocked {
            // Chromium reports no URL on its first walk, so rows may exist.
            forget_focus(&db, focus_event.take(), focus_first_frame.take())?;
            event_pending = true; // written if the window later shows an allowed page
        } else {
            if event_pending && (frame.is_some() || window_ax.is_err()) {
                db.execute(
                    "INSERT INTO events (ts, app, title) VALUES (?1, ?2, ?3)",
                    (now_ms(), app, title),
                )?;
                focus_event = Some(db.last_insert_rowid());
                event_pending = false;
            }
            if let Some((text, url, nodes)) = frame.filter(|(text, ..)| dedupe.is_new(text)) {
                db.execute(
                    "INSERT INTO frames (ts, app, title, url, text) VALUES (?1, ?2, ?3, ?4, ?5)",
                    (now_ms(), app, title, &url, &text),
                )?;
                focus_first_frame.get_or_insert(db.last_insert_rowid());
                if debug {
                    eprintln!("frame: {app}, {nodes} nodes, {} bytes, url: {}", text.len(), url.is_some());
                }
            }
        }
        last_focus = focus;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn private_windows_are_refused() {
        assert!(window_denied("New Tab - Google Chrome (Incognito)"));
        assert!(window_denied("Example — Mozilla Firefox Private Browsing"));
        assert!(window_denied("nithin — deepmem-capture — 80×24"));
        assert!(!window_denied("deep-memory/tasks/todo.md - Google Chrome"));
    }

    #[test]
    fn a_blocked_window_loses_the_rows_already_written_for_it() {
        let db = Connection::open_in_memory().unwrap();
        db.execute_batch(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, title TEXT);
             CREATE TABLE frames (id INTEGER PRIMARY KEY, text TEXT);
             INSERT INTO events (title) VALUES ('editor'), ('My Bank');
             INSERT INTO frames (text) VALUES ('code'), ('loading'), ('balance');",
        )
        .unwrap();
        let rows = |table: &str| -> Vec<i64> {
            let mut query = db.prepare(&format!("SELECT id FROM {table} ORDER BY id")).unwrap();
            let ids = query.query_map([], |row| row.get(0)).unwrap();
            ids.map(Result::unwrap).collect()
        };
        forget_focus(&db, None, None).unwrap();
        assert_eq!((rows("events"), rows("frames")), (vec![1, 2], vec![1, 2, 3]));
        forget_focus(&db, Some(2), Some(2)).unwrap();
        assert_eq!((rows("events"), rows("frames")), (vec![1], vec![1]));
    }

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
