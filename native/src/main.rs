use iroh::endpoint::QuicTransportConfig;
use iroh::{endpoint::presets, Endpoint, RelayMode};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::env;
use std::sync::{
    atomic::{AtomicU64, Ordering},
    Arc,
};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::sync::{mpsc, Mutex};
use tokio::time::{timeout, Duration};

const ALPN: &[u8] = b"goose-acp/1";
type Writers = Arc<Mutex<HashMap<String, mpsc::Sender<String>>>>;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let bind = env::var("ACP_STUDIO_BIND").unwrap_or_else(|_| "127.0.0.1:43187".into());
    let advertised = env::var("ACP_STUDIO_ADDRESS").unwrap_or_else(|_| bind.clone());
    let token = env::var("ACP_STUDIO_TOKEN")?;
    let endpoint = Endpoint::builder(presets::Minimal)
        .relay_mode(RelayMode::Disabled)
        .clear_ip_transports()
        .bind_addr(bind)?
        .alpns(vec![ALPN.to_vec()])
        .transport_config(
            QuicTransportConfig::builder()
                .default_path_keep_alive_interval(Duration::from_secs(1))
                .default_path_max_idle_timeout(Duration::from_secs(3))
                .build(),
        )
        .bind()
        .await?;
    let endpoint_id: String = endpoint
        .id()
        .as_bytes()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    let (events, mut output) = mpsc::channel::<Value>(256);
    tokio::spawn(async move {
        let mut stdout = tokio::io::stdout();
        while let Some(event) = output.recv().await {
            let line = format!("{event}\n");
            if stdout.write_all(line.as_bytes()).await.is_err() || stdout.flush().await.is_err() {
                break;
            }
        }
    });
    events.send(json!({"kind":"ready","pairing":{
        "version":1,"name":"ACP Studio","endpointID":endpoint_id,"address":advertised,"token":token
    }})).await?;
    let writers: Writers = Arc::new(Mutex::new(HashMap::new()));
    let commands = writers.clone();
    let command_task = tokio::spawn(async move {
        let mut lines = BufReader::new(tokio::io::stdin()).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            let Ok(command) = serde_json::from_str::<Value>(&line) else {
                continue;
            };
            let Some(connection) = command["connection"].as_str() else {
                continue;
            };
            let sender = commands.lock().await.get(connection).cloned();
            if let Some(sender) = sender {
                let _ = sender.send(command["message"].to_string()).await;
            }
        }
    });
    let serial = AtomicU64::new(1);
    loop {
        tokio::select! {
            _ = command_task_finished(&command_task) => break,
            incoming = endpoint.accept() => {
                let Some(incoming) = incoming else { break };
                let connection_id = serial.fetch_add(1, Ordering::Relaxed).to_string();
                let events = events.clone();
                let writers = writers.clone();
                let token = token.clone();
                let endpoint_id = endpoint_id.clone();
                tokio::spawn(async move {
                    let result = serve(incoming, &connection_id, &token, &endpoint_id, writers.clone(), events.clone()).await;
                    writers.lock().await.remove(&connection_id);
                    let _ = events.send(json!({"kind":"closed","connection":connection_id,
                        "error":result.err().map(|error|error.to_string())})).await;
                });
            }
        }
    }
    endpoint.close().await;
    Ok(())
}

async fn command_task_finished(task: &tokio::task::JoinHandle<()>) {
    while !task.is_finished() {
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

async fn serve(
    incoming: iroh::endpoint::Incoming,
    connection_id: &str,
    token: &str,
    endpoint_id: &str,
    writers: Writers,
    events: mpsc::Sender<Value>,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let connection = timeout(Duration::from_secs(10), incoming).await??;
    let (mut send, mut recv) = timeout(Duration::from_secs(10), connection.accept_bi()).await??;
    let hello = timeout(Duration::from_secs(10), async {
        let length = recv.read_u32_le().await? as usize;
        if length > 65536 {
            return Err(std::io::Error::other("Hello too large"));
        }
        let mut bytes = vec![0; length];
        AsyncReadExt::read_exact(&mut recv, &mut bytes).await?;
        serde_json::from_slice::<Value>(&bytes).map_err(std::io::Error::other)
    })
    .await??;
    let accepted = hello["studio_version"] == 1 && hello["token"].as_str() == Some(token);
    let ack = if accepted {
        json!({"Accepted":{"agent_id":endpoint_id}})
    } else {
        json!({"Rejected":{"code":"STUDIO_PAIRING_REJECTED"}})
    };
    let bytes = ack.to_string().into_bytes();
    send.write_u32_le(bytes.len() as u32).await?;
    send.write_all(&bytes).await?;
    send.flush().await?;
    if !accepted {
        let _ = send.finish();
        return Ok(());
    }
    let (messages, mut outgoing) = mpsc::channel::<String>(128);
    writers.lock().await.insert(connection_id.into(), messages);
    events
        .send(json!({"kind":"connected","connection":connection_id,
        "peer":connection.remote_id().to_string()}))
        .await?;
    let mut reader = BufReader::new(recv);
    let reading = async {
        loop {
            let mut line = String::new();
            if reader.read_line(&mut line).await? == 0 {
                return Ok::<(), std::io::Error>(());
            }
            if line.len() > 8 * 1024 * 1024 {
                return Err(std::io::Error::other("ACP message too large"));
            }
            let message: Value = serde_json::from_str(&line).map_err(std::io::Error::other)?;
            events
                .send(json!({"kind":"message","connection":connection_id,"message":message}))
                .await
                .map_err(std::io::Error::other)?;
        }
    };
    let writing = async {
        while let Some(message) = outgoing.recv().await {
            send.write_all(message.as_bytes()).await?;
            send.write_all(b"\n").await?;
            send.flush().await?;
        }
        Ok::<(), std::io::Error>(())
    };
    tokio::select! { result = reading => result?, result = writing => result? }
    connection.close(0u32.into(), b"studio session ended");
    Ok(())
}
