//! deepmem-capture: records the focused app and window title into SQLite.
//! Contract with the Python side: this daemon only writes raw tables in
//! ~/.deepmem/db.sqlite (override with DEEPMEM_DB).

use accessibility_sys::{
    kAXErrorSuccess, AXError, AXIsProcessTrusted, AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication, AXUIElementRef,
};
use core_foundation::base::{CFType, CFTypeRef, TCFType};
use core_foundation::runloop::{kCFRunLoopDefaultMode, CFRunLoop};
use objc2_app_kit::NSWorkspace;
use core_foundation::string::CFString;
use rusqlite::Connection;
use std::path::PathBuf;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use std::{env, fs, ptr};

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

/// Title of the focused window of process `pid`. Err carries the AXError code.
fn window_title(pid: i32) -> Result<String, AXError> {
    unsafe {
        let app = CFType::wrap_under_create_rule(AXUIElementCreateApplication(pid) as CFTypeRef);
        let window = attr(app.as_CFTypeRef() as AXUIElementRef, "AXFocusedWindow")?;
        let title = attr(window.as_CFTypeRef() as AXUIElementRef, "AXTitle")?;
        Ok(title.downcast::<CFString>().map(|s| s.to_string()).unwrap_or_default())
    }
}

/// (app name, pid) of the frontmost app. Needs no permission, but only stays
/// current while the main run loop is pumped (see the loop in `main`).
fn frontmost() -> Option<(String, i32)> {
    let app = NSWorkspace::sharedWorkspace().frontmostApplication()?;
    let name = app.localizedName().map(|n| n.to_string()).unwrap_or_default();
    Some((name, app.processIdentifier()))
}

fn db_path() -> PathBuf {
    env::var_os("DEEPMEM_DB").map(PathBuf::from).unwrap_or_else(|| {
        PathBuf::from(env::var_os("HOME").expect("HOME not set")).join(".deepmem/db.sqlite")
    })
}

fn main() -> rusqlite::Result<()> {
    if !unsafe { AXIsProcessTrusted() } {
        eprintln!(
            "deepmem-capture: no Accessibility permission, recording app names \
             without window titles.\nSystem Settings > Privacy & Security > \
             Accessibility: enable the terminal (or app) that runs this binary, \
             then start it again."
        );
    }

    let path = db_path();
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir).expect("cannot create database directory");
    }
    let db = Connection::open(&path)?;
    db.pragma_update(None, "journal_mode", "WAL")?;
    db.execute(
        "CREATE TABLE IF NOT EXISTS events (
            id    INTEGER PRIMARY KEY,
            ts    INTEGER NOT NULL, -- unix milliseconds
            app   TEXT NOT NULL,
            title TEXT NOT NULL
        )",
        [],
    )?;
    eprintln!("deepmem-capture: writing to {}", path.display());

    let mut last: Option<(String, String)> = None;
    let mut last_error: AXError = kAXErrorSuccess;
    // ponytail: 500 ms polling; replace with AXObserver notifications in Slice 2
    // when capture becomes event-driven.
    loop {
        if let Some((app, pid)) = frontmost() {
            let title = window_title(pid).unwrap_or_else(|err| {
                if err != last_error {
                    eprintln!("deepmem-capture: no window title for {app} (AXError {err})");
                    last_error = err;
                }
                String::new()
            });
            let now = Some((app, title));
            if now != last {
                let (app, title) = now.as_ref().unwrap();
                let ts = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as i64;
                db.execute(
                    "INSERT INTO events (ts, app, title) VALUES (?1, ?2, ?3)",
                    (ts, app, title),
                )?;
                last = now;
            }
        }
        // Pumping the run loop (instead of sleeping) keeps NSWorkspace current.
        CFRunLoop::run_in_mode(unsafe { kCFRunLoopDefaultMode }, Duration::from_millis(500), false);
    }
}
