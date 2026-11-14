//! Replaces secrets with `[REDACTED:kind]` before anything is written to disk.
//!
//! The same rules exist in Python (`memory/src/deepmem/store.py`, `redact`),
//! which applies them again to everything it indexes or returns. Both are
//! tested against the shared fixtures in `testdata/`; change them together.

use regex::{Captures, Regex};
use std::sync::LazyLock;

static SECRETS: LazyLock<Vec<(&str, Regex)>> = LazyLock::new(|| {
    [
        ("private-key", r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\z)"),
        ("aws-key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        ("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b"),
        ("api-key", r"\bsk-[A-Za-z0-9_-]{20,}"),
        ("api-key", r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{16,}\b"),
        ("api-key", r"\bAIza[0-9A-Za-z_-]{35,}"),
        ("slack-token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
        ("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
        ("token", r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}=*"),
    ]
    .into_iter()
    .map(|(kind, pattern)| (kind, Regex::new(pattern).unwrap()))
    .collect()
});

/// `password = hunter2hunter2`, `API_KEY: "..."`, `?token=...`: keep the name, drop the value.
static ASSIGNMENT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r#"(?i)\b([a-z0-9_-]*(?:api[_-]?key|secret|token|passw(?:or)?d|pwd)[a-z0-9_-]*)(\s*[:=]\s*['"]?)([^\s'"&]{8,})"#,
    )
    .unwrap()
});

static CARD: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\b\d(?:[ -]?\d){12,18}\b").unwrap());

fn luhn_ok(candidate: &str) -> bool {
    let digits = candidate.bytes().filter(u8::is_ascii_digit).rev();
    let sum: u32 = digits
        .enumerate()
        .map(|(i, d)| {
            let n = (d - b'0') as u32;
            match (i % 2, n) {
                (0, _) => n,
                (_, 5..) => n * 2 - 9,
                _ => n * 2,
            }
        })
        .sum();
    sum % 10 == 0
}

// ponytail: pattern list only catches known shapes; a random-looking secret
// with no label passes. Add entropy detection if leaks show up.
pub fn redact(text: &str) -> String {
    let mut text = text.to_string();
    for (kind, pattern) in SECRETS.iter() {
        if pattern.is_match(&text) {
            text = pattern.replace_all(&text, format!("[REDACTED:{kind}]")).into_owned();
        }
    }
    let text = ASSIGNMENT.replace_all(&text, |c: &Captures| {
        if c[3].starts_with("[REDACTED") {
            c[0].to_string()
        } else {
            format!("{}{}[REDACTED:secret]", &c[1], &c[2])
        }
    });
    CARD.replace_all(&text, |c: &Captures| {
        if luhn_ok(&c[0]) { "[REDACTED:card]".to_string() } else { c[0].to_string() }
    })
    .into_owned()
}

#[cfg(test)]
mod tests {
    use super::redact;

    #[test]
    fn removes_every_shared_fixture_secret_and_keeps_normal_text() {
        let screen = include_str!("../../testdata/redaction_screen.txt");
        let out = redact(screen);
        for secret in include_str!("../../testdata/redaction_secrets.txt").lines() {
            assert!(!out.contains(secret), "leaked: {secret}");
        }
        assert!(out.contains("the deploy checklist is done"));
        assert!(out.contains("password: [REDACTED:secret]"));

        // Not secrets: ordinary numbers, short values, a number failing the card checksum.
        let plain = "order 1234 5678 9012 3456 took 1700000000000 ms, token count: 42";
        assert_eq!(redact(plain), plain);
        assert_eq!(redact(&out), out); // redacting twice changes nothing
    }
}
