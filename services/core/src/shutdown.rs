/// Resolves on SIGINT or SIGTERM. Docker sends SIGTERM on `docker stop`, and a
/// PID-1 process without a handler ignores it until it is SIGKILLed.
pub async fn shutdown_signal() {
    let ctrl_c = tokio::signal::ctrl_c();

    #[cfg(unix)]
    {
        use tokio::signal::unix::{signal, SignalKind};
        let mut term = signal(SignalKind::terminate()).expect("failed to install SIGTERM handler");
        tokio::select! {
            _ = ctrl_c => {}
            _ = term.recv() => {}
        }
    }

    #[cfg(not(unix))]
    {
        let _ = ctrl_c.await;
    }

    tracing::info!(event = "shutdown_signal_received");
}
