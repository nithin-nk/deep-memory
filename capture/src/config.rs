//! `~/.deepmem/config.toml`: the rules for what is never recorded (read by
//! the capture loop) and the settings the window can change. The Python side
//! reads the same file; its defaults are repeated in `SETTINGS`.

use serde_json::{json, Map, Value};
use std::fs;
use std::path::Path;
use toml_edit::{Array, DocumentMut, Item};

/// Matched as case-insensitive substrings of the app name.
pub const DEFAULT_DENYLIST: [&str; 13] = [
    "1password", "bitwarden", "lastpass", "dashlane", "keepass", "keychain access",
    "passwords", "enpass", "proton pass", "nordpass", "keeper", "strongbox",
    "loginwindow", // lock screen
];

/// What must never be recorded.
#[derive(Debug, Default, PartialEq)]
pub struct Rules {
    /// Built-in denylist plus `denylist = [...]`, lower case.
    pub denylist: Vec<String>,
    /// `url_blocklist = [...]`, each entry normalised by `site`.
    pub url_blocklist: Vec<String>,
    /// `screenshots = false` turns the screenshot fallback off.
    pub screenshots: bool,
    /// A window with less content text than this gets a screenshot.
    pub screenshot_below_chars: usize,
}

/// A window with less content text than this is treated as showing its
/// content only as pixels. Labels of toolbars, menus and buttons do not count:
/// Preview showing a picture exposes about 250 characters of them. Override
/// with `screenshot_below_chars`.
pub const SCREENSHOT_BELOW_CHARS: usize = 200;

fn read(home: &Path) -> Result<DocumentMut, String> {
    let path = home.join("config.toml");
    let text = match fs::read_to_string(&path) {
        Ok(text) => text,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => String::new(),
        Err(err) => return Err(format!("cannot read {}: {err}", path.display())),
    };
    text.parse().map_err(|err| format!("cannot parse {}: {err}", path.display()))
}

fn strings(doc: &DocumentMut, key: &str) -> Vec<String> {
    let array = doc.get(key).and_then(Item::as_array);
    array.into_iter().flatten().filter_map(|v| v.as_str()).map(str::to_string).collect()
}

/// An error means the file cannot be parsed. The caller must not carry on as
/// if there were no rules: privacy settings are never ignored silently.
pub fn load_rules(home: &Path) -> Result<Rules, String> {
    let doc = read(home)?;
    let mut denylist: Vec<String> = DEFAULT_DENYLIST.iter().map(|s| s.to_string()).collect();
    denylist.extend(strings(&doc, "denylist").iter().map(|s| s.to_lowercase()));
    let url_blocklist = strings(&doc, "url_blocklist").iter().filter_map(|s| site(s)).collect();
    let screenshots = doc.get("screenshots").and_then(Item::as_bool).unwrap_or(true);
    let screenshot_below_chars = doc
        .get("screenshot_below_chars")
        .and_then(Item::as_integer)
        .map_or(SCREENSHOT_BELOW_CHARS, |n| n.max(0) as usize);
    Ok(Rules { denylist, url_blocklist, screenshots, screenshot_below_chars })
}

pub fn app_denied(app: &str, denylist: &[String]) -> bool {
    let app = app.to_lowercase();
    denylist.iter().any(|entry| !entry.is_empty() && app.contains(entry.as_str()))
}

/// `https://www.Bank.com:443/Login/?next=x` -> `bank.com/login`. The same
/// form is used for blocklist entries and for the pages compared with them.
/// None when there is no host (`file:///…`, empty input).
// ponytail: no IPv6 literals and no IDN handling; add when someone blocks one.
pub fn site(url: &str) -> Option<String> {
    let url = url.trim().to_lowercase();
    let rest = url.split_once("://").map_or(url.as_str(), |(_, rest)| rest);
    let rest = rest.split(['?', '#']).next().unwrap_or("");
    let (authority, path) = rest.split_once('/').unwrap_or((rest, ""));
    let host = authority.rsplit('@').next().unwrap_or("").split(':').next().unwrap_or("");
    let host = host.strip_prefix("www.").unwrap_or(host);
    let valid = host.contains('.') || host == "localhost";
    if !valid || host.contains(char::is_whitespace) {
        return None;
    }
    let path = path.trim_end_matches('/');
    Some(if path.is_empty() { host.to_string() } else { format!("{host}/{path}") })
}

/// True when `url` is on a blocked site: the same host or a subdomain of it,
/// and under the entry's path when the entry has one.
pub fn url_blocked(url: &str, blocklist: &[String]) -> bool {
    let Some(page) = site(url) else { return false };
    let (host, path) = page.split_once('/').unwrap_or((&page, ""));
    blocklist.iter().any(|entry| {
        let (entry_host, entry_path) = entry.split_once('/').unwrap_or((entry, ""));
        let host_matches = host == entry_host
            || host.strip_suffix(entry_host).is_some_and(|sub| sub.ends_with('.'));
        let path_matches = entry_path.is_empty()
            || path.strip_prefix(entry_path).is_some_and(|more| more.is_empty() || more.starts_with('/'));
        host_matches && path_matches
    })
}

// --- settings shown in the window -------------------------------------------

/// Keys the window can change, with the defaults the Python side uses
/// (`CONFIG_DEFAULTS` in memory/src/deepmem/store.py).
fn defaults() -> Value {
    json!({
        "model": "qwen3-vl:8b-instruct",
        "ollama_url": "http://localhost:11434",
        "num_ctx": 16384,
        "session_gap_minutes": 5,
        "session_max_minutes": 30,
        "distill_on_battery": false,
        "denylist": [],
        "url_blocklist": [],
    })
}

/// Current settings: the file's values where they have the right type,
/// defaults elsewhere.
pub fn get(home: &Path) -> Result<Value, String> {
    let doc = read(home)?;
    let mut settings = defaults();
    for (key, value) in settings.as_object_mut().unwrap() {
        let Some(item) = doc.get(key) else { continue };
        let found = match value {
            Value::String(_) => item.as_str().map(Value::from),
            Value::Number(_) => item.as_integer().map(Value::from),
            Value::Bool(_) => item.as_bool().map(Value::from),
            _ => item.as_array().map(|_| Value::from(strings(&doc, key))),
        };
        if let Some(found) = found {
            *value = found;
        }
    }
    Ok(json!({ "settings": settings, "builtin_denylist": DEFAULT_DENYLIST }))
}

/// Captured text must not leave this machine: same rule as `call_ollama` in
/// memory/src/deepmem/distill.py.
fn is_local_url(url: &str) -> bool {
    let Some(rest) = url.strip_prefix("http://").or_else(|| url.strip_prefix("https://")) else {
        return false;
    };
    let authority = rest.split('/').next().unwrap_or("");
    let host = match authority.strip_prefix('[') {
        Some(v6) => v6.split(']').next().unwrap_or(""),
        None => authority.split(':').next().unwrap_or(""),
    };
    !authority.contains('@') && matches!(host, "localhost" | "127.0.0.1" | "::1")
}

fn string_list(value: &Value) -> Option<Vec<String>> {
    value.as_array()?.iter().map(|v| v.as_str().map(|s| s.trim().to_string())).collect()
}

/// Checks one new value and returns it in the form that is written to the file.
fn validated(key: &str, value: &Value) -> Result<Item, String> {
    let item = match key {
        "model" => {
            let model = value.as_str().map(str::trim).filter(|m| !m.is_empty());
            toml_edit::value(model.ok_or("Model name cannot be empty.")?)
        }
        "ollama_url" => {
            let url = value.as_str().map(str::trim).filter(|u| is_local_url(u)).ok_or(
                "Ollama URL must point at this machine (http://localhost, 127.0.0.1 or [::1]).",
            )?;
            toml_edit::value(url)
        }
        "num_ctx" | "session_gap_minutes" | "session_max_minutes" => {
            let limit = if key == "num_ctx" { 1_000_000 } else { 24 * 60 };
            let number = value.as_i64().filter(|n| (1..=limit).contains(n));
            toml_edit::value(number.ok_or(format!("{key} must be a whole number from 1 to {limit}."))?)
        }
        "distill_on_battery" => toml_edit::value(value.as_bool().ok_or("Expected on or off.")?),
        "denylist" => {
            let mut apps = string_list(value).ok_or("Expected a list of app names.")?;
            apps.retain(|app| !app.is_empty());
            apps.dedup();
            toml_edit::value(Array::from_iter(apps))
        }
        "url_blocklist" => {
            let mut sites = Vec::new();
            for entry in string_list(value).ok_or("Expected a list of sites.")? {
                let site = site(&entry).ok_or(format!("\"{entry}\" is not a website address."))?;
                if !sites.contains(&site) {
                    sites.push(site);
                }
            }
            toml_edit::value(Array::from_iter(sites))
        }
        _ => return Err(format!("Unknown setting: {key}")),
    };
    Ok(item)
}

/// Changes the given keys and leaves the rest of the file, including the
/// user's comments and keys this program does not know, as it was. Nothing
/// is written unless every value is valid.
pub fn set(home: &Path, changes: &Map<String, Value>) -> Result<(), String> {
    let mut doc = read(home)?;
    for (key, value) in changes {
        doc[key.as_str()] = validated(key, value)?;
    }
    // Write beside the file, then rename: the daemon and the Python side
    // never see a half-written config.
    let path = home.join("config.toml");
    let temp = home.join("config.toml.tmp");
    fs::write(&temp, doc.to_string())
        .and_then(|_| fs::rename(&temp, &path))
        .map_err(|err| format!("cannot write {}: {err}", path.display()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_home(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("deepmem-test-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn denylisted_apps_are_refused() {
        let denylist: Vec<String> =
            DEFAULT_DENYLIST.iter().map(|s| s.to_string()).chain(["signal".to_string()]).collect();
        assert!(app_denied("1Password 7", &denylist));
        assert!(app_denied("Keychain Access", &denylist));
        assert!(app_denied("Signal", &denylist));
        assert!(!app_denied("Google Chrome", &denylist));
        assert!(!app_denied("Terminal", &denylist));
        assert!(!app_denied("Terminal", &["".to_string()]));
    }

    #[test]
    fn sites_are_normalised() {
        assert_eq!(site("https://www.Bank.com:443/Login/?next=x#top").as_deref(), Some("bank.com/login"));
        assert_eq!(site("bank.com").as_deref(), Some("bank.com"));
        assert_eq!(site("  http://user@mail.example.com/ ").as_deref(), Some("mail.example.com"));
        assert_eq!(site("http://localhost:3000/app").as_deref(), Some("localhost/app"));
        assert_eq!(site("file:///Users/me/notes.html"), None);
        assert_eq!(site("not a site"), None);
        assert_eq!(site(""), None);
    }

    #[test]
    fn blocked_sites_match_host_subdomains_and_path() {
        let list = ["bank.com".to_string(), "example.org/private".to_string()];
        assert!(url_blocked("https://bank.com/", &list));
        assert!(url_blocked("https://www.bank.com/accounts?id=1", &list));
        assert!(url_blocked("https://online.bank.com:8443/login", &list));
        assert!(!url_blocked("https://notbank.com/", &list));
        assert!(!url_blocked("https://bank.com.evil.net/", &list));
        assert!(url_blocked("https://example.org/private", &list));
        assert!(url_blocked("https://example.org/Private/notes", &list));
        assert!(!url_blocked("https://example.org/privateer", &list));
        assert!(!url_blocked("https://example.org/public", &list));
        assert!(!url_blocked("file:///tmp/bank.com", &list));
        assert!(!url_blocked("https://bank.com/", &[]));
    }

    #[test]
    fn rules_come_from_the_config_file() {
        let home = temp_home("rules");
        let defaults = load_rules(&home).unwrap();
        assert_eq!(defaults.url_blocklist, Vec::<String>::new());
        assert_eq!((defaults.screenshots, defaults.screenshot_below_chars), (true, 200));
        fs::write(
            home.join("config.toml"),
            "denylist = [\"Signal\"]\nurl_blocklist = [\"https://www.Bank.com/\", \"junk\"]\n\
             screenshots = false\nscreenshot_below_chars = 50\n",
        )
        .unwrap();
        let rules = load_rules(&home).unwrap();
        assert!(app_denied("Signal", &rules.denylist));
        assert!(app_denied("1Password", &rules.denylist));
        assert_eq!(rules.url_blocklist, ["bank.com"]);
        assert_eq!((rules.screenshots, rules.screenshot_below_chars), (false, 50));

        fs::write(home.join("config.toml"), "denylist = [").unwrap();
        assert!(load_rules(&home).is_err());
    }

    #[test]
    fn saving_keeps_comments_and_unknown_keys() {
        let home = temp_home("save");
        fs::write(
            home.join("config.toml"),
            "# my notes\nmodel = \"old\"\nfuture_key = 7\n\n# apps\ndenylist = [\"Signal\"]\n",
        )
        .unwrap();
        let changes = json!({
            "model": " new-model ",
            "num_ctx": 8192,
            "distill_on_battery": true,
            "url_blocklist": ["https://www.bank.com/login", "bank.com/login", "example.org"],
        });
        set(&home, changes.as_object().unwrap()).unwrap();

        let text = fs::read_to_string(home.join("config.toml")).unwrap();
        assert!(text.contains("# my notes") && text.contains("# apps") && text.contains("future_key = 7"));
        let settings = &get(&home).unwrap()["settings"];
        assert_eq!(settings["model"], "new-model");
        assert_eq!(settings["num_ctx"], 8192);
        assert_eq!(settings["distill_on_battery"], true);
        assert_eq!(settings["session_gap_minutes"], 5); // default
        assert_eq!(settings["denylist"], json!(["Signal"]));
        assert_eq!(settings["url_blocklist"], json!(["bank.com/login", "example.org"]));
    }

    #[test]
    fn invalid_values_are_refused_and_nothing_is_written() {
        let home = temp_home("invalid");
        for bad in [
            json!({"ollama_url": "http://example.com:11434"}),
            json!({"ollama_url": "http://localhost@example.com"}),
            json!({"ollama_url": "localhost:11434"}),
            json!({"model": "  "}),
            json!({"num_ctx": 0}),
            json!({"session_gap_minutes": "5"}),
            json!({"url_blocklist": ["bank.com", "not a site"]}),
            json!({"model": "fine", "surprise": 1}),
        ] {
            assert!(set(&home, bad.as_object().unwrap()).is_err(), "{bad}");
        }
        assert!(!home.join("config.toml").exists());
        for good in ["http://localhost:11434", "http://127.0.0.1:11434/", "http://[::1]:11434"] {
            set(&home, json!({"ollama_url": good}).as_object().unwrap()).unwrap();
        }
    }
}
