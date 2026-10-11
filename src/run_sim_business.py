import uvicorn

if __name__ == "__main__":
    # Match the workstation's explicit loop factory; uvicorn overrides event policies.
    uvicorn.run(
        "commerce.sim_business:app",
        host="127.0.0.1",
        port=8081,
        loop="core.loops:selector_loop_factory",
    )
