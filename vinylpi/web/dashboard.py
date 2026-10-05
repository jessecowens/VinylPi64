from .app import create_app
from .services.auto_wake_monitor import start as start_auto_wake_monitor
from .services.auto_wake_monitor import stop as stop_auto_wake_monitor

app = create_app()

if __name__ == "__main__":
    start_auto_wake_monitor()
    try:
        app.run(host="0.0.0.0", port=8080)
    finally:
        stop_auto_wake_monitor()
