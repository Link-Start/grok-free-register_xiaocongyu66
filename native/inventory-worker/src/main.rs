//! inventory-worker — Rust account inventory for grok-free-register.
//!
//! Modes:
//!   inventory-worker scan    --keys-dir keys [--json]
//!   inventory-worker rebuild --keys-dir keys
//!   inventory-worker check   --keys-dir keys   (exit 0 if keys dir usable)
//!   inventory-worker version

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::env;
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process;
use std::time::SystemTime;

#[derive(Debug, Clone, Serialize)]
struct AccountRecord {
    id: String,
    email: String,
    status: String,
    formats: Vec<String>,
    has_sso: bool,
    has_access_token: bool,
    has_refresh_token: bool,
    subject: String,
    fingerprint: String,
    created_at: String,
    updated_at: String,
    paths: BTreeMap<String, String>,
    ledger_state: String,
    source: String,
}

#[derive(Debug, Serialize)]
struct ScanSummary {
    total: usize,
    by_status: BTreeMap<String, usize>,
    by_format: BTreeMap<String, usize>,
    artifacts: BTreeMap<String, bool>,
    export_dir: String,
    generated_at: String,
    engine: String,
}

fn main() {
    let mut args: Vec<String> = env::args().skip(1).collect();
    if args.is_empty() {
        usage();
        process::exit(2);
    }
    let cmd = args.remove(0);
    match cmd.as_str() {
        "version" | "--version" | "-V" => {
            println!("inventory-worker {}", env!("CARGO_PKG_VERSION"));
        }
        "check" => {
            let keys = parse_keys_dir(&args);
            if !keys.is_dir() {
                // create empty layout so gate can pass after first build
                if let Err(e) = fs::create_dir_all(&keys) {
                    eprintln!("[inventory-worker] cannot create {}: {e}", keys.display());
                    process::exit(1);
                }
            }
            println!(
                "{}",
                json!({
                    "ok": true,
                    "engine": "rust",
                    "keys_dir": keys.display().to_string(),
                    "version": env!("CARGO_PKG_VERSION"),
                })
            );
        }
        "scan" => {
            let keys = parse_keys_dir(&args);
            let as_json = args.iter().any(|a| a == "--json" || a == "-j");
            let records = scan_accounts(&keys);
            let summary = inventory_summary(&keys, &records);
            if as_json {
                let out = json!({
                    "ok": true,
                    "engine": "rust",
                    "summary": summary,
                    "accounts": records,
                });
                println!("{}", serde_json::to_string_pretty(&out).unwrap());
            } else {
                println!(
                    "engine=rust total={} ready={} pending={} dir={}",
                    summary.total,
                    summary.by_status.get("oauth_ready").copied().unwrap_or(0),
                    summary
                        .by_status
                        .get("oauth_pending")
                        .copied()
                        .unwrap_or(0),
                    summary.export_dir
                );
            }
        }
        "rebuild" => {
            let keys = parse_keys_dir(&args);
            match rebuild_all(&keys) {
                Ok(paths) => {
                    println!(
                        "{}",
                        json!({
                            "ok": true,
                            "engine": "rust",
                            "paths": paths,
                        })
                    );
                }
                Err(e) => {
                    eprintln!("[inventory-worker] rebuild failed: {e}");
                    process::exit(1);
                }
            }
        }
        "help" | "--help" | "-h" => usage(),
        other => {
            eprintln!("unknown command: {other}");
            usage();
            process::exit(2);
        }
    }
}

fn usage() {
    let _ = writeln!(
        io::stderr(),
        "usage: inventory-worker <scan|rebuild|check|version> [--keys-dir DIR] [--json]"
    );
}

fn parse_keys_dir(args: &[String]) -> PathBuf {
    let mut keys = env::var("KEY_EXPORT_DIR").unwrap_or_else(|_| "keys".into());
    let mut i = 0;
    while i < args.len() {
        if args[i] == "--keys-dir" || args[i] == "-d" {
            if i + 1 < args.len() {
                keys = args[i + 1].clone();
                i += 2;
                continue;
            }
        } else if let Some(rest) = args[i].strip_prefix("--keys-dir=") {
            keys = rest.to_string();
        }
        i += 1;
    }
    let p = PathBuf::from(&keys);
    if p.is_absolute() {
        p
    } else {
        env::current_dir().unwrap_or_else(|_| PathBuf::from(".")).join(p)
    }
}

fn mtime_iso(path: &Path) -> String {
    path.metadata()
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| t.duration_since(SystemTime::UNIX_EPOCH).ok())
        .map(|d| {
            DateTime::<Utc>::from_timestamp(d.as_secs() as i64, 0)
                .map(|dt| dt.to_rfc3339())
                .unwrap_or_default()
        })
        .unwrap_or_default()
}

fn read_json(path: &Path) -> Option<Value> {
    let text = fs::read_to_string(path).ok()?;
    serde_json::from_str(&text).ok()
}

fn scan_accounts(root: &Path) -> Vec<AccountRecord> {
    let mut by_email: HashMap<String, AccountRecord> = HashMap::new();

    // legacy accounts.txt
    let accounts_txt = root.join("accounts.txt");
    if accounts_txt.is_file() {
        if let Ok(text) = fs::read_to_string(&accounts_txt) {
            for line in text.lines() {
                let line = line.trim();
                if line.is_empty() || line.starts_with('#') {
                    continue;
                }
                let parts: Vec<&str> = line.split(':').collect();
                if parts.len() < 2 {
                    continue;
                }
                let email = parts[0].trim().to_string();
                if email.is_empty() {
                    continue;
                }
                let sso = if parts.len() >= 3 {
                    parts[2].trim().to_string()
                } else {
                    String::new()
                };
                let rec = by_email.entry(email.clone()).or_insert_with(|| AccountRecord {
                    id: email.clone(),
                    email: email.clone(),
                    status: if sso.is_empty() {
                        "unknown".into()
                    } else {
                        "legacy_sso".into()
                    },
                    formats: vec![],
                    has_sso: false,
                    has_access_token: false,
                    has_refresh_token: false,
                    subject: String::new(),
                    fingerprint: String::new(),
                    created_at: String::new(),
                    updated_at: String::new(),
                    paths: BTreeMap::new(),
                    ledger_state: String::new(),
                    source: String::new(),
                });
                if !rec.formats.iter().any(|f| f == "legacy") {
                    rec.formats.push("legacy".into());
                }
                rec.has_sso = rec.has_sso || !sso.is_empty();
                rec.paths
                    .insert("legacy".into(), accounts_txt.display().to_string());
                if rec.updated_at.is_empty() {
                    rec.updated_at = mtime_iso(&accounts_txt);
                }
                if rec.source.is_empty() {
                    rec.source = "accounts.txt".into();
                }
                if !sso.is_empty() && rec.status == "unknown" {
                    rec.status = "legacy_sso".into();
                }
            }
        }
    }

    // sub2api singles
    let sub_dir = root.join("sub2api");
    if sub_dir.is_dir() {
        if let Ok(rd) = fs::read_dir(&sub_dir) {
            let mut paths: Vec<PathBuf> = rd
                .filter_map(|e| e.ok().map(|e| e.path()))
                .filter(|p| {
                    p.extension().and_then(|x| x.to_str()) == Some("json")
                        && p.file_name()
                            .and_then(|n| n.to_str())
                            .map(|n| n.ends_with(".sub2api.json") && n != "accounts.sub2api.json")
                            .unwrap_or(false)
                })
                .collect();
            paths.sort();
            for path in paths {
                let Some(doc) = read_json(&path) else { continue };
                let accounts = doc.get("accounts").and_then(|a| a.as_array()).cloned().unwrap_or_default();
                let fp = path
                    .file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or("")
                    .trim_end_matches(".sub2api.json")
                    .to_string();
                for item in accounts {
                    let creds = item.get("credentials").cloned().unwrap_or(json!({}));
                    let extra = item.get("extra").cloned().unwrap_or(json!({}));
                    let email = creds
                        .get("email")
                        .or_else(|| extra.get("email"))
                        .or_else(|| item.get("name"))
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .trim()
                        .to_string();
                    if email.is_empty() {
                        continue;
                    }
                    let rec = by_email.entry(email.clone()).or_insert_with(|| AccountRecord {
                        id: email.clone(),
                        email: email.clone(),
                        status: "oauth_ready".into(),
                        formats: vec![],
                        has_sso: false,
                        has_access_token: false,
                        has_refresh_token: false,
                        subject: String::new(),
                        fingerprint: String::new(),
                        created_at: String::new(),
                        updated_at: String::new(),
                        paths: BTreeMap::new(),
                        ledger_state: String::new(),
                        source: String::new(),
                    });
                    if !rec.formats.iter().any(|f| f == "sub2api") {
                        rec.formats.push("sub2api".into());
                    }
                    rec.has_access_token =
                        rec.has_access_token || creds.get("access_token").and_then(|v| v.as_str()).map(|s| !s.is_empty()).unwrap_or(false);
                    rec.has_refresh_token =
                        rec.has_refresh_token || creds.get("refresh_token").and_then(|v| v.as_str()).map(|s| !s.is_empty()).unwrap_or(false);
                    if rec.subject.is_empty() {
                        rec.subject = extra
                            .get("subject")
                            .or_else(|| creds.get("sub"))
                            .and_then(|v| v.as_str())
                            .unwrap_or("")
                            .to_string();
                    }
                    if rec.fingerprint.is_empty() {
                        rec.fingerprint = fp.clone();
                    }
                    rec.paths
                        .insert("sub2api".into(), path.display().to_string());
                    let mt = mtime_iso(&path);
                    if mt > rec.updated_at {
                        rec.updated_at = mt;
                    }
                    if rec.created_at.is_empty() {
                        rec.created_at = doc
                            .get("exported_at")
                            .and_then(|v| v.as_str())
                            .unwrap_or("")
                            .to_string();
                    }
                    rec.status = "oauth_ready".into();
                }
            }
        }
    }

    // cpa singles
    let cpa_dir = root.join("cpa");
    if cpa_dir.is_dir() {
        if let Ok(rd) = fs::read_dir(&cpa_dir) {
            let mut paths: Vec<PathBuf> = rd
                .filter_map(|e| e.ok().map(|e| e.path()))
                .filter(|p| {
                    p.extension().and_then(|x| x.to_str()) == Some("json")
                        && p.file_name()
                            .and_then(|n| n.to_str())
                            .map(|n| n.starts_with("xai-") && n.ends_with(".json"))
                            .unwrap_or(false)
                })
                .collect();
            paths.sort();
            // purge legacy merge bundles if present
            for bad in ["accounts.cpa.json", "accounts.cpa.zip"] {
                let p = cpa_dir.join(bad);
                let _ = fs::remove_file(p);
            }
            for path in paths {
                let Some(doc) = read_json(&path) else { continue };
                let mut email = doc
                    .get("email")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .trim()
                    .to_string();
                if email.is_empty() {
                    email = doc
                        .get("name")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .trim()
                        .to_string();
                }
                if email.is_empty() {
                    email = path
                        .file_stem()
                        .and_then(|s| s.to_str())
                        .unwrap_or("unknown")
                        .to_string();
                }
                let fp = path
                    .file_stem()
                    .and_then(|s| s.to_str())
                    .unwrap_or("")
                    .to_string();
                let rec = by_email.entry(email.clone()).or_insert_with(|| AccountRecord {
                    id: email.clone(),
                    email: email.clone(),
                    status: "oauth_ready".into(),
                    formats: vec![],
                    has_sso: false,
                    has_access_token: false,
                    has_refresh_token: false,
                    subject: String::new(),
                    fingerprint: String::new(),
                    created_at: String::new(),
                    updated_at: String::new(),
                    paths: BTreeMap::new(),
                    ledger_state: String::new(),
                    source: String::new(),
                });
                if !rec.formats.iter().any(|f| f == "cpa") {
                    rec.formats.push("cpa".into());
                }
                rec.has_access_token = rec.has_access_token
                    || doc
                        .get("access_token")
                        .and_then(|v| v.as_str())
                        .map(|s| !s.is_empty())
                        .unwrap_or(false);
                rec.has_refresh_token = rec.has_refresh_token
                    || doc
                        .get("refresh_token")
                        .and_then(|v| v.as_str())
                        .map(|s| !s.is_empty())
                        .unwrap_or(false);
                if rec.subject.is_empty() {
                    rec.subject = doc
                        .get("sub")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string();
                }
                if rec.fingerprint.is_empty() {
                    rec.fingerprint = fp;
                }
                rec.paths.insert("cpa".into(), path.display().to_string());
                let mt = mtime_iso(&path);
                if mt > rec.updated_at {
                    rec.updated_at = mt;
                }
                if rec.status != "oauth_ready"
                    && (rec.has_access_token || rec.has_refresh_token)
                {
                    rec.status = "oauth_ready".into();
                }
            }
        }
    }

    for rec in by_email.values_mut() {
        if rec.has_sso
            && !rec.has_access_token
            && !rec.has_refresh_token
            && rec.formats.iter().any(|f| f == "legacy")
            && !rec.formats.iter().any(|f| f == "sub2api")
            && !rec.formats.iter().any(|f| f == "cpa")
        {
            rec.status = "oauth_pending".into();
        }
        if rec.formats.is_empty() {
            rec.formats.push("unknown".into());
        }
        rec.formats.sort();
        rec.formats.dedup();
    }

    let mut records: Vec<AccountRecord> = by_email.into_values().collect();
    let order = |s: &str| match s {
        "oauth_ready" => 0,
        "oauth_pending" => 1,
        "legacy_sso" => 2,
        _ => 9,
    };
    records.sort_by(|a, b| {
        order(&a.status)
            .cmp(&order(&b.status))
            .then_with(|| b.updated_at.cmp(&a.updated_at))
            .then_with(|| a.email.cmp(&b.email))
    });
    records
}

fn inventory_summary(root: &Path, records: &[AccountRecord]) -> ScanSummary {
    let mut by_status: BTreeMap<String, usize> = BTreeMap::new();
    let mut by_format: BTreeMap<String, usize> = BTreeMap::new();
    for r in records {
        *by_status.entry(r.status.clone()).or_default() += 1;
        for f in &r.formats {
            *by_format.entry(f.clone()).or_default() += 1;
        }
    }
    let mut artifacts = BTreeMap::new();
    artifacts.insert(
        "legacy_accounts_txt".into(),
        root.join("accounts.txt").is_file(),
    );
    artifacts.insert(
        "sub2api_bundle".into(),
        root.join("sub2api/accounts.sub2api.json").is_file(),
    );
    // merge bundles permanently removed
    artifacts.insert("cpa_bundle_json".into(), false);
    artifacts.insert("cpa_bundle_zip".into(), false);
    let cpa_singles = fs::read_dir(root.join("cpa"))
        .ok()
        .map(|rd| {
            rd.filter_map(|e| e.ok())
                .filter(|e| {
                    e.file_name()
                        .to_str()
                        .map(|n| n.starts_with("xai-") && n.ends_with(".json"))
                        .unwrap_or(false)
                })
                .count()
        })
        .unwrap_or(0);
    artifacts.insert("cpa_singles".into(), cpa_singles > 0);
    ScanSummary {
        total: records.len(),
        by_status,
        by_format,
        artifacts,
        export_dir: root.display().to_string(),
        generated_at: Utc::now().to_rfc3339(),
        engine: "rust".into(),
    }
}

fn atomic_write_json(path: &Path, value: &Value) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension(format!(
        "{}.tmp",
        path.extension().and_then(|e| e.to_str()).unwrap_or("json")
    ));
    let text = serde_json::to_string_pretty(value).map_err(|e| io::Error::new(io::ErrorKind::Other, e))?;
    fs::write(&tmp, text + "\n")?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&tmp, fs::Permissions::from_mode(0o600));
    }
    fs::rename(&tmp, path)?;
    Ok(())
}

fn rebuild_sub2api(root: &Path) -> io::Result<PathBuf> {
    let directory = root.join("sub2api");
    fs::create_dir_all(&directory)?;
    let mut accounts: Vec<Value> = Vec::new();
    let mut seen: BTreeSet<String> = BTreeSet::new();
    if let Ok(rd) = fs::read_dir(&directory) {
        let mut paths: Vec<PathBuf> = rd
            .filter_map(|e| e.ok().map(|e| e.path()))
            .filter(|p| {
                p.file_name()
                    .and_then(|n| n.to_str())
                    .map(|n| n.ends_with(".sub2api.json") && n != "accounts.sub2api.json")
                    .unwrap_or(false)
            })
            .collect();
        paths.sort();
        for path in paths {
            let Some(doc) = read_json(&path) else { continue };
            let items = doc.get("accounts").and_then(|a| a.as_array()).cloned().unwrap_or_default();
            for item in items {
                let creds = item.get("credentials").cloned().unwrap_or(json!({}));
                let key = format!(
                    "{}|{}|{}|{}",
                    item.get("platform").and_then(|v| v.as_str()).unwrap_or(""),
                    creds.get("refresh_token").and_then(|v| v.as_str()).unwrap_or(""),
                    creds.get("access_token").and_then(|v| v.as_str()).unwrap_or(""),
                    item.get("name").and_then(|v| v.as_str()).unwrap_or(""),
                );
                if !seen.insert(key) {
                    continue;
                }
                accounts.push(item);
            }
        }
    }
    let out = json!({
        "exported_at": Utc::now().to_rfc3339(),
        "proxies": [],
        "accounts": accounts,
        "engine": "rust",
    });
    let target = directory.join("accounts.sub2api.json");
    atomic_write_json(&target, &out)?;
    Ok(target)
}

fn rebuild_cpa(root: &Path) -> io::Result<()> {
    // Permanently removed: never write accounts.cpa.json / accounts.cpa.zip.
    // Only purge leftovers; singles (xai-*.json) are the CPA product.
    let directory = root.join("cpa");
    fs::create_dir_all(&directory)?;
    for bad in ["accounts.cpa.json", "accounts.cpa.zip", "accounts.cpa.zip.tmp"] {
        let p = directory.join(bad);
        let _ = fs::remove_file(p);
    }
    Ok(())
}

fn rebuild_all(root: &Path) -> io::Result<BTreeMap<String, String>> {
    let mut paths = BTreeMap::new();
    let sub = rebuild_sub2api(root)?;
    paths.insert("sub2api_json".into(), sub.display().to_string());
    rebuild_cpa(root)?;
    let cpa_dir = root.join("cpa");
    if cpa_dir.is_dir() {
        let count = fs::read_dir(&cpa_dir)
            .ok()
            .map(|rd| {
                rd.filter_map(|e| e.ok())
                    .filter(|e| {
                        e.file_name()
                            .to_str()
                            .map(|n| n.starts_with("xai-") && n.ends_with(".json"))
                            .unwrap_or(false)
                    })
                    .count()
            })
            .unwrap_or(0);
        if count > 0 {
            paths.insert("cpa_dir".into(), cpa_dir.display().to_string());
            paths.insert("cpa_singles".into(), count.to_string());
        }
    }
    let legacy = root.join("accounts.txt");
    if legacy.is_file() {
        paths.insert("legacy_txt".into(), legacy.display().to_string());
    }
    Ok(paths)
}

// silence unused import if walkdir not used in slim build path
#[allow(dead_code)]
fn _walkdir_touch() {
    let _ = walkdir::WalkDir::new(".");
}
