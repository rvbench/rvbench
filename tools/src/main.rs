use anyhow::{Result, ensure};
use clap::{Parser, Subcommand};
use rvbench_tools::{bundle, ssh, trace};
use std::{
    io::Write,
    path::{Path, PathBuf},
    process::Command,
    time::Duration,
};

#[derive(Parser)]
#[command(
    name = "rvbench-tools",
    about = "Rust checks, board bundles and SSH maintenance"
)]
struct Args {
    #[arg(long, global = true)]
    root: Option<PathBuf>,
    #[command(subcommand)]
    command: Action,
}
#[derive(Subcommand)]
enum Action {
    Check,
    Bundle {
        #[arg(long)]
        output: PathBuf,
        /// Checkout of the standalone rvbench-spacemit-worker repository.
        #[arg(long)]
        worker_root: PathBuf,
        /// Checkout of the standalone rvbench-official-manager repository.
        /// Omit for a worker-only bundle.
        #[arg(long)]
        manager_root: Option<PathBuf>,
        #[arg(long)]
        binary: Option<PathBuf>,
    },
    Trace {
        #[arg(long, conflicts_with = "dir")]
        root: Option<PathBuf>,
        #[arg(long)]
        dir: Option<PathBuf>,
        #[arg(long)]
        json: bool,
    },
    Pssh {
        target: String,
        command: Option<String>,
        #[arg(long)]
        user: Option<String>,
        #[arg(long)]
        key: Option<PathBuf>,
        #[arg(long)]
        no_config: bool,
        #[arg(long)]
        accept_new: bool,
        #[arg(long, default_value_t = 15)]
        connect_timeout: u64,
        #[arg(long, default_value_t = 120)]
        timeout: u64,
        #[arg(long,num_args=2,value_names=["LOCAL","REMOTE"])]
        put: Option<Vec<String>>,
        #[arg(long,num_args=2,value_names=["REMOTE","LOCAL"])]
        get: Option<Vec<String>>,
    },
    Precompile {
        target: String,
        candidate: PathBuf,
        #[arg(long)]
        op: String,
        #[arg(long, required = true)]
        shape: Vec<String>,
        #[arg(long, default_value = "float32")]
        dtype: String,
        #[arg(long)]
        user: Option<String>,
        #[arg(long)]
        key: Option<PathBuf>,
        #[arg(long)]
        accept_new: bool,
        #[arg(long, default_value = "/home/c4y/autoke/tmp_precompile")]
        remote_root: String,
        #[arg(long, default_value = "/home/c4y/autoke/scripts/run_env.sh")]
        run_env: String,
        #[arg(long, default_value_t = 600)]
        timeout: u64,
    },
}
fn repo(root: &Option<PathBuf>) -> PathBuf {
    root.clone().unwrap_or_else(|| {
        let cwd = std::env::current_dir().unwrap_or_default();
        cwd.ancestors()
            .find(|p| p.join("core/Cargo.toml").is_file())
            .map(Path::to_owned)
            .unwrap_or_else(|| {
                PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                    .parent()
                    .unwrap()
                    .to_owned()
            })
    })
}
fn check(root: &Path) -> Result<()> {
    // Windows 不允许替换正在运行的 exe，而下面的 `cargo test` / `cargo clippy` 会重写
    // `target/debug/rvbench-tools.exe`：正是当前进程自己。改用独立 target 目录，检查
    // 不再踩到自己的可执行文件；代价是首次多一遍编译。
    let target = root.join("target").join("check");
    for args in [
        vec!["fmt", "--all", "--", "--check"],
        vec!["test", "--workspace", "--locked"],
        vec![
            "clippy",
            "--workspace",
            "--all-targets",
            "--locked",
            "--",
            "-D",
            "warnings",
        ],
    ] {
        ensure!(
            Command::new("cargo")
                .args(&args)
                .current_dir(root)
                .env("CARGO_TARGET_DIR", &target)
                .status()?
                .success(),
            "cargo check failed"
        );
    }
    let public_base = root.join("examples/hello-world");
    let public = rvbench_cli::load_json(public_base.join("spec.json"))?;
    ensure!(
        rvbench_cli::validate_spec(&public, Some(&public_base)).is_empty(),
        "public spec invalid"
    );
    let result = rvbench_cli::load_json(public_base.join("eval_results.json"))?;
    ensure!(
        rvbench_cli::validate_eval_results(&result, Some(&public), None, Some(&public_base))
            .is_empty(),
        "synthetic result invalid"
    );
    let baseline_base = root.join("examples/baseline");
    let baseline_spec = rvbench_cli::load_json(baseline_base.join("spec.json"))?;
    let baseline_result = rvbench_cli::load_json(baseline_base.join("baseline-results.json"))?;
    ensure!(
        rvbench_cli::validate_baseline_results(
            &baseline_result,
            Some(&baseline_spec),
            Some(&baseline_base),
            Some(&baseline_base),
        )
        .is_empty(),
        "baseline example invalid"
    );
    println!("VALID: Rust workspace and public contracts");
    Ok(())
}
#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    let root = repo(&args.root);
    match args.command {
        Action::Check => check(&root)?,
        Action::Bundle {
            output,
            worker_root,
            manager_root,
            binary,
        } => {
            let manifest = bundle::build_bundle(
                &root,
                &worker_root,
                manager_root.as_deref(),
                &output,
                binary.as_deref(),
            )?;
            println!(
                "Built {} files in {}",
                manifest["files"].as_array().unwrap().len(),
                output.display()
            );
        }
        Action::Trace { root, dir, json } => {
            let base = root
                .or(dir)
                .ok_or_else(|| anyhow::anyhow!("pass --root or --dir"))?;
            let result = trace::check(&base)?;
            if json {
                println!("{}", serde_json::to_string_pretty(&result)?);
            } else {
                println!(
                    "files={} errors={} warnings={}",
                    result["summary"]["files"],
                    result["summary"]["errors"],
                    result["summary"]["warnings"]
                );
            }
            ensure!(result["ok"] == true, "trace contract violations");
        }
        Action::Pssh {
            target,
            command,
            user,
            key,
            no_config,
            accept_new,
            connect_timeout,
            timeout,
            put,
            get,
        } => {
            ensure!(put.is_none() || get.is_none(), "choose upload or download");
            let resolved = ssh::resolve(&target, user.as_deref(), key.as_deref(), no_config)?;
            let session =
                ssh::Session::connect(&resolved, accept_new, Duration::from_secs(connect_timeout))
                    .await?;
            if let Some(paths) = put {
                tokio::time::timeout(
                    Duration::from_secs(timeout),
                    session.put(Path::new(&paths[0]), &paths[1]),
                )
                .await??;
                println!("PUT_OK {} -> {}", paths[0], paths[1]);
            } else if let Some(paths) = get {
                tokio::time::timeout(
                    Duration::from_secs(timeout),
                    session.get(&paths[0], Path::new(&paths[1])),
                )
                .await??;
                println!("GET_OK {} -> {}", paths[0], paths[1]);
            } else {
                let (code, out, err) = session
                    .exec(&command.unwrap_or_default(), Duration::from_secs(timeout))
                    .await?;
                std::io::stdout().write_all(&out)?;
                std::io::stderr().write_all(&err)?;
                println!("EXIT={code}");
                session.close().await?;
                ensure!(code == 0, "remote command failed with exit {code}");
                return Ok(());
            }
            session.close().await?;
        }
        Action::Precompile {
            target,
            candidate,
            op,
            shape,
            dtype,
            user,
            key,
            accept_new,
            remote_root,
            run_env,
            timeout,
        } => {
            ensure!(
                regex::Regex::new(r"^[a-z_][a-z0-9_]{0,47}$")?.is_match(&op),
                "invalid op"
            );
            ensure!(
                ["float32", "float16", "int32", "int64", "bool"].contains(&dtype.as_str()),
                "invalid dtype"
            );
            ensure!(
                shape.len() <= 16
                    && shape
                        .iter()
                        .all(|s| regex::Regex::new(r"^[0-9]{1,10}(,[0-9]{1,10}){0,5}$")
                            .unwrap()
                            .is_match(s)),
                "invalid shape"
            );
            let resolved = ssh::resolve(&target, user.as_deref(), key.as_deref(), false)?;
            let session =
                ssh::Session::connect(&resolved, accept_new, Duration::from_secs(15)).await?;
            let directory = format!(
                "{}/{}",
                remote_root.trim_end_matches('/'),
                uuid::Uuid::new_v4().simple()
            );
            let quote = shell_quote;
            let (code, _, stderr) = session
                .exec(
                    &format!("mkdir -p {}", quote(&directory)),
                    Duration::from_secs(30),
                )
                .await?;
            ensure!(
                code == 0,
                "cannot create precompile directory: {}",
                String::from_utf8_lossy(&stderr)
            );
            let candidate_name = candidate
                .file_name()
                .and_then(|s| s.to_str())
                .ok_or_else(|| anyhow::anyhow!("invalid candidate filename"))?;
            let remote_candidate = format!("{directory}/{candidate_name}");
            let remote_driver = format!("{directory}/driver.py");
            session.put(&candidate, &remote_candidate).await?;
            session
                .put(
                    &root.join("backends/spacemit/k3/adapters/python/precompile_driver.py"),
                    &remote_driver,
                )
                .await?;
            let command = format!(
                "source {} && python {} {} {} {} {}",
                quote(&run_env),
                quote(&remote_driver),
                quote(&remote_candidate),
                quote(&op),
                quote(&dtype),
                quote(&serde_json::to_string(&shape)?)
            );
            let result = session.exec(&command, Duration::from_secs(timeout)).await;
            if let Ok(sftp) = session.sftp().await {
                let _ = sftp.remove_file(&remote_candidate).await;
                let _ = sftp.remove_file(&remote_driver).await;
                let _ = sftp.remove_dir(&directory).await;
                let _ = sftp.close().await;
            }
            let (code, out, err) = result?;
            std::io::stdout().write_all(&out)?;
            std::io::stderr().write_all(&err)?;
            session.close().await?;
            ensure!(code == 0, "precompile driver exited with code {code}");
        }
    }
    Ok(())
}

fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\"'\"'"))
}
