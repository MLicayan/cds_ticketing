from app import create_app, socketio

app = create_app()

if __name__ == "__main__":
    # app.run(debug=True)
    cfg = app.config
    socketio.run(
        app,
        host=cfg.get("HOST", "0.0.0.0"),
        port=cfg.get("PORT", 5013),
        debug=cfg.get("DEBUG", True),
        use_reloader=cfg.get("USE_RELOADER", False),
        allow_unsafe_werkzeug=True,
    )
