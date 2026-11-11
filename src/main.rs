use clap::{Parser, Subcommand};
use graph_contracts_poc::provider;
use serde_json::{Value, json};
use std::fs::File;
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::time::Duration;

#[derive(Parser)]
#[command(
    version,
    about = "Explore graph observations, native/CUE contracts and declared reuse"
)]
struct Cli {
    #[arg(long, global = true, default_value = "cue")]
    cue: PathBuf,
    #[arg(long, global = true, default_value_t = 5000)]
    timeout_ms: u64,
    #[command(subcommand)]
    command: Operation,
}

#[derive(Subcommand)]
enum Operation {
    /// Validate and summarize a graph snapshot.
    Inspect {
        #[arg(long)]
        graph: PathBuf,
    },
    /// List outbound recorded edges.
    Neighbors {
        #[arg(long)]
        graph: PathBuf,
        #[arg(long)]
        subject: String,
        #[arg(long)]
        relation: Option<String>,
    },
    /// Find a bounded shortest path in recorded relationships.
    Path {
        #[arg(long)]
        graph: PathBuf,
        #[arg(long)]
        from: String,
        #[arg(long)]
        to: String,
        #[arg(long)]
        relation: Option<String>,
        #[arg(long, default_value_t = 8)]
        max_depth: usize,
    },
    /// Produce a candidate contract, retaining its observation identity.
    Derive {
        #[arg(long)]
        graph: PathBuf,
        #[arg(long)]
        subject: String,
    },
    /// Check an input using a backend's native contract document.
    Check {
        #[arg(long, default_value = "native")]
        backend: String,
        #[arg(long)]
        contract: PathBuf,
        #[arg(long)]
        input: PathBuf,
        /// Exit 3 unless the result is satisfied. Default: return every report normally.
        #[arg(long)]
        require_satisfied: bool,
    },
    /// Compare authored native and CUE contracts on the same input.
    Compare {
        #[arg(long)]
        native: PathBuf,
        #[arg(long)]
        cue_contract: PathBuf,
        #[arg(long)]
        input: PathBuf,
    },
    /// Match declared capabilities and expose residual and coupling obligations.
    Match {
        #[arg(long)]
        request: PathBuf,
    },
    /// Emit CLI provider discovery metadata.
    Discover,
    /// Read one Xorchestrate CLI-provider request from stdin and emit one result.
    Provider,
}

fn read_bounded(reader: impl Read) -> Result<String, String> {
    let mut bytes = Vec::new();
    reader
        .take(4_194_305)
        .read_to_end(&mut bytes)
        .map_err(|error| error.to_string())?;
    if bytes.len() > 4_194_304 {
        return Err("Input exceeds 4 MiB".to_owned());
    }
    String::from_utf8(bytes).map_err(|error| error.to_string())
}

fn read_text(path: &Path) -> Result<String, String> {
    read_bounded(File::open(path).map_err(|error| format!("{}: {error}", path.display()))?)
}

fn read_json(path: &Path) -> Result<Value, String> {
    serde_json::from_str(&read_text(path)?).map_err(|error| format!("{}: {error}", path.display()))
}

fn contract(path: &Path, backend: &str) -> Result<Value, String> {
    if backend == "cue" && path.extension().is_some_and(|extension| extension == "cue") {
        Ok(json!({"source":read_text(path)?,"definition":"#Contract"}))
    } else {
        read_json(path)
    }
}

fn run(cli: Cli) -> Result<(Value, bool), String> {
    if cli.timeout_ms == 0 {
        return Err("timeout-ms must be positive".to_owned());
    }
    let timeout = Duration::from_millis(cli.timeout_ms);
    let invoke = |name, arguments: Value| provider::execute(name, &arguments, &cli.cue, timeout);
    match cli.command {
        Operation::Inspect { graph } => Ok((
            invoke("graph.inspect", json!({"graph":read_json(&graph)?}))?,
            true,
        )),
        Operation::Neighbors {
            graph,
            subject,
            relation,
        } => {
            let mut arguments = json!({"graph":read_json(&graph)?,"subject":subject});
            if let Some(relation) = relation {
                arguments["relation"] = json!(relation);
            }
            Ok((invoke("graph.neighbors", arguments)?, true))
        }
        Operation::Path {
            graph,
            from,
            to,
            relation,
            max_depth,
        } => {
            let mut arguments =
                json!({"graph":read_json(&graph)?,"from":from,"to":to,"max_depth":max_depth});
            if let Some(relation) = relation {
                arguments["relation"] = json!(relation);
            }
            Ok((invoke("graph.path", arguments)?, true))
        }
        Operation::Derive { graph, subject } => Ok((
            invoke(
                "contract.derive",
                json!({"graph":read_json(&graph)?,"subject":subject}),
            )?,
            true,
        )),
        Operation::Check {
            backend,
            contract: path,
            input,
            require_satisfied,
        } => {
            let result = invoke(
                "contract.evaluate",
                json!({
                    "backend":backend,"contract":contract(&path,&backend)?,"input":read_json(&input)?
                }),
            )?;
            let success = !require_satisfied
                || result.get("status").and_then(Value::as_str) == Some("satisfied");
            Ok((result, success))
        }
        Operation::Compare {
            native,
            cue_contract,
            input,
        } => Ok((
            invoke(
                "contract.compare",
                json!({
                    "native":contract(&native,"native")?,"cue":contract(&cue_contract,"cue")?,"input":read_json(&input)?
                }),
            )?,
            true,
        )),
        Operation::Match { request } => Ok((
            invoke("reuse.analyze", json!({"request":read_json(&request)?}))?,
            true,
        )),
        Operation::Discover => Ok((provider::discover(), true)),
        Operation::Provider => {
            let request: Value = serde_json::from_str(&read_bounded(io::stdin().lock())?)
                .map_err(|error| error.to_string())?;
            Ok((provider::dispatch(&request, &cli.cue, timeout)?, true))
        }
    }
}

fn main() -> ExitCode {
    match run(Cli::parse()) {
        Ok((result, success)) => {
            println!(
                "{}",
                serde_json::to_string_pretty(&result).expect("JSON result")
            );
            if success {
                ExitCode::SUCCESS
            } else {
                ExitCode::from(3)
            }
        }
        Err(message) => {
            println!(
                "{}",
                json!({"error":{"code":"invalid_request","message":message}})
            );
            ExitCode::from(2)
        }
    }
}
