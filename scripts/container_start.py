"""Idempotent initialization of the isolated container demo, then serve Vue."""

import os
import runpy


def main():
    if os.environ.get("POSTGRES_DB") != "agent_commerce_container_demo":
        raise RuntimeError("Container initializer requires its dedicated demonstration database")
    from commerce.acceptance_seed import seed_acceptance
    from commerce.migrate import migrate
    from commerce.seed import seed
    from commerce.seed_1301 import seed_preview_policies

    print({"migrations": migrate(), "seed": seed(), "policies": seed_preview_policies()})
    print({"acceptance": seed_acceptance()})
    runpy.run_path("src/run_service.py", run_name="__main__")


if __name__ == "__main__":
    main()
