use rhombus::challenge_loader_plugin::ChallengeLoaderPlugin;
use std::path::PathBuf;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env()
                .or_else(|_| EnvFilter::try_new("rhombus=info,realistic_demo=info"))
                .unwrap(),
        )
        .init();

    let app = rhombus::Builder::default()
        .load_env()
        .config_source(rhombus::config::File::with_name("config"))
        // This demo only listens on loopback. Router::serve does not attach
        // Axum ConnectInfo, so provide the known local client IP explicitly;
        // the optional per-IP rate limiter then has a stable key to enforce.
        .extractor(|_, _| Some(std::net::IpAddr::V4(std::net::Ipv4Addr::LOCALHOST)))
        .plugin(ChallengeLoaderPlugin::new(PathBuf::from("challenges")))
        .build()
        .await
        .expect("failed to build realistic demo");

    // The demo is intentionally loopback-only. Change this explicitly if you
    // intend to expose it, and rotate every secret in config.yaml first.
    let listener = tokio::net::TcpListener::bind("127.0.0.1:3000")
        .await
        .expect("failed to bind http://127.0.0.1:3000");
    app.serve(listener).await;
}
