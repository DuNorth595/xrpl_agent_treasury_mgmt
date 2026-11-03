#!/usr/bin/env python3
"""
One-command environment for the XRPL treasury service.

WHY THIS EXISTS
===============

Until now the test environment was a set of processes started by hand in
different terminals, with URLs copied between them. That is fine for
one person poking at it and useless for anyone else, and it cannot be
trusted to be in the same state twice.

This script owns the whole lifecycle:

    up      bring the environment up and wait until it is READY
    test    run the full test suite against the running environment
    down    tear it down
    status  report what is running and what is not

The environment has four parts:

    1. the XRP Ledger devnet   (Docker: xrpl-up, rippleci/xrpld:3.4.1)
    2. the reference ERP        (a local HTTP service on port 8096)
    3. the treasury service     (a local HTTP service on port 8095)
    4. the test harness         (api_test.py, run against the above)

READINESS
=========

"Running" is not "ready". The ledger is ready when it answers
server_info over its websocket. The ERP is ready when /health answers.
The treasury is ready when /api/health answers. Each is polled with a
timeout, and a failure names the component that never came up rather
than reporting a generic failure.

SAFETY
======

    - The ledger devnet is a LOCAL Docker network. Nothing here talks to
      a public ledger.
    - The treasury service is bound to the address given by --host,
      which defaults to 127.0.0.1 (loopback only).
    - `test --allow-move` submits a real transaction, but only on the
      local devnet, and the harness refuses a non-local base URL.
    - `down` stops only what this script started, by PID file.

USAGE
=====

    python3 dev_env.py up
    python3 dev_env.py test
    python3 dev_env.py test --allow-move
    python3 dev_env.py status
    python3 dev_env.py down
    python3 dev_env.py cycle --allow-move     # up, test, down
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

# The service package lives under src/. It is imported here for the
# credential minting, so the path has to be set before that import.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

HERE = pathlib.Path(__file__).resolve().parent
STATE_DIR = HERE / ".dev-env"
LOG_DIR = STATE_DIR / "logs"
PID_FILE = STATE_DIR / "services.json"

ERP_PORT = int(os.environ.get("TREASURY_ERP_PORT", "8096"))
SERVICE_PORT = int(os.environ.get("TREASURY_SERVICE_PORT", "8095"))
DEFAULT_HOST = os.environ.get("TREASURY_HOST", "127.0.0.1")
RIPPLE_WS = os.environ.get("TREASURY_RIPPLE_WS", "ws://127.0.0.1:6006")
FAUCET_URL = os.environ.get("TREASURY_FAUCET_URL", "http://127.0.0.1:3001/faucet")

ERP_COMMAND = [
    sys.executable,
    str(HERE / "reference_erp.py"),
    "--port",
    str(ERP_PORT),
]

# The local signing service - the stand-in for a custodian or KMS. The
# treasury service holds no key when it is pointed at this.
SIGNER_PORT = int(os.environ.get("TREASURY_SIGNER_PORT", "8098"))
SIGNER_TOKEN = os.environ.get("SIGNER_SERVICE_TOKEN", "local-signer-token")
SIGNER_COMMAND = [
    sys.executable,
    str(HERE / "local_signer_service.py"),
    "--port",
    str(SIGNER_PORT),
    "--token",
    SIGNER_TOKEN,
    "--log",
    str(HERE / "var" / "signer-audit.jsonl"),
]

SERVICE_COMMAND = [
    sys.executable,
    str(HERE / "run_demo.py"),
    "--host",
    "{host}",
    "--port",
    str(SERVICE_PORT),
    "--erp-url",
    "http://127.0.0.1:{erp_port}",
]

# The devnet's API credentials. They are minted into a file with mode
# 0600, outside the repository, and passed to the service through the
# environment. The service reads TREASURY_API_TOKENS; the harness reads
# the same file so both agree on the credentials.
CREDENTIAL_FILE = pathlib.Path.home() / ".treasury-dev" / "api-tokens.json"


def ensure_credentials() -> Dict[str, str]:
    """Mint the devnet's API credentials if they do not exist.

    Three credentials, deliberately scoped differently:

        reader      can see the position, the plan and the log
        authoriser  can also record a sign-off
        operator    can also submit a real payment

    A read token that could authorise would be a read token that could
    authorise, so the scopes are separate. The tokens are minted ONCE
    and reused, so the harness and the service keep agreeing across
    restarts.
    """
    import json as _json

    if CREDENTIAL_FILE.exists():
        try:
            stored = _json.loads(CREDENTIAL_FILE.read_text())
            if stored:
                return stored
        except Exception:
            pass

    CREDENTIAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    # The DIRECTORY is 0700, so only this account can list it. The
    # directory has to be tightened before the file is created - a
    # file cannot be chmod'd before it exists.
    CREDENTIAL_FILE.parent.chmod(0o700)

    from xrpl_agent_treasury_mgmt.auth import mint_credential

    credentials = {}
    for name, scopes, description in (
        (
            "reader",
            ["treasury:read"],
            "can see the position, the plan and the decision log",
        ),
        (
            "authoriser",
            ["treasury:read", "treasury:authorise", "treasury:propose"],
            "can propose a dry run and record a sign-off",
        ),
        (
            "operator",
            [
                "treasury:read",
                "treasury:propose",
                "treasury:authorise",
                "treasury:settle",
            ],
            "can submit a real payment to the ledger",
        ),
    ):
        credential, token = mint_credential(
            name=name,
            scopes=scopes,
            description=description,
        )
        credentials[name] = {
            "token": token,
            "scopes": sorted(scopes),
            "description": description,
        }

    CREDENTIAL_FILE.write_text(_json.dumps(credentials, indent=2))
    CREDENTIAL_FILE.chmod(0o600)
    return credentials


# --------------------------------------------------------------- utilities


def log(message: str, level: str = "info") -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"  {stamp}  {message}", flush=True)


def http_ok(url: str, timeout: float = 4.0) -> Tuple[bool, Optional[Dict]]:
    """GET a URL and parse JSON. Returns (ok, body)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
        try:
            return True, json.loads(body)
        except json.JSONDecodeError:
            return True, {"_raw": body[:500]}
    except urllib.error.HTTPError as exc:
        try:
            return False, json.loads(exc.read().decode("utf-8", errors="replace"))
        except Exception:
            return False, {"_status": exc.code}
    except Exception:
        return False, None


def ledger_ok(timeout: float = 5.0) -> bool:
    """Is the ledger answering over its websocket?"""
    try:
        import asyncio

        import websockets

        async def probe():
            async with websockets.connect(RIPPLE_WS) as ws:
                await ws.send(json.dumps({"command": "server_info"}))
                response = json.loads(await ws.recv())
                return response.get("status") == "success"

        return asyncio.run(asyncio.wait_for(probe(), timeout=timeout))
    except Exception:
        return False


def ledger_is_validator() -> bool:
    """Is the devnet leading consensus rather than observing?

    A node that is merely observing answers server_info but refuses every
    account with InsufficientNetworkMode. The server_state field says
    which mode the node is in.
    """
    try:
        import asyncio

        import websockets

        async def probe():
            async with websockets.connect(RIPPLE_WS) as ws:
                await ws.send(json.dumps({"command": "server_info"}))
                response = json.loads(await ws.recv())
                info = response.get("result", {}).get("info", {})
                return info.get("server_state") in ("proposing", "full")

        return asyncio.run(asyncio.wait_for(probe(), timeout=5.0))
    except Exception:
        return False


def ledger_advancing() -> bool:
    """Is the ledger's index moving?

    A node can be proposing and still be stuck: if the two validators
    are on different networks they never agree, and the ledger sits at
    its first ledgers forever. Comparing the index across two calls
    catches that.
    """
    try:
        import asyncio

        import websockets

        async def index():
            async with websockets.connect(RIPPLE_WS) as ws:
                await ws.send(json.dumps({"command": "ledger_current"}))
                response = json.loads(await ws.recv())
                return int(
                    response.get("result", {}).get("ledger_current_index", 0)
                )

        async def probe():
            first = await index()
            await asyncio.sleep(4)
            second = await index()
            return second > first

        return asyncio.run(asyncio.wait_for(probe(), timeout=20.0))
    except Exception:
        return False


def wait_for(
    name: str,
    probe,
    timeout: float = 60.0,
    interval: float = 1.0,
) -> bool:
    """Poll a probe until it passes or the timeout expires."""
    deadline = time.time() + timeout
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        try:
            if probe():
                log(f"{name} is ready ({attempts} attempt{'s' if attempts != 1 else ''})")
                return True
        except Exception:
            pass
        time.sleep(interval)
    log(f"{name} did NOT become ready within {timeout:.0f}s", "error")
    return False


# ------------------------------------------------------------- process mgmt


def read_state() -> Dict:
    if not PID_FILE.exists():
        return {}
    try:
        return json.loads(PID_FILE.read_text())
    except Exception:
        return {}


def write_state(state: Dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(json.dumps(state, indent=2))


def alive(pid: int) -> bool:
    """Is the process still running?

    A process this script started becomes a ZOMBIE when it exits,
    because nothing calls wait() on it. os.kill(pid, 0) succeeds on a
    zombie, so a naive check reports a dead process as alive. That made
    every shutdown wait out the full timeout and then escalate to
    SIGKILL on a process that had already stopped.

    The zombie is detected by reading its state out of the process
    table, and it is reaped at the same time.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False

    # It exists. Is it a zombie?
    try:
        state = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        if state.startswith("Z"):
            # A zombie has exited. Reap it so the table does not fill.
            try:
                os.waitpid(pid, os.WNOHANG)
            except Exception:
                pass
            return False
    except Exception:
        pass
    return True


def start_service(
    name: str,
    command: List[str],
    env: Optional[Dict] = None,
) -> int:
    """Start a service as a detached child and record its PID."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{name}.log"
    handle = open(log_path, "ab")

    process_env = dict(os.environ)
    if env:
        process_env.update(env)

    # Start in its own session so it survives the parent and can be
    # stopped as a group.
    process = subprocess.Popen(
        command,
        stdout=handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=process_env,
        start_new_session=True,
        cwd=str(HERE),
    )
    log(f"started {name} (pid {process.pid}) -> {log_path}")
    return process.pid


def stop_service(name: str, pid: int, timeout: float = 8.0) -> bool:
    if not alive(pid):
        log(f"{name} (pid {pid}) was not running")
        return True
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not alive(pid):
            # Reap it so it does not linger as a zombie.
            try:
                os.waitpid(pid, os.WNOHANG)
            except Exception:
                pass
            log(f"stopped {name}")
            return True
        time.sleep(0.2)

    # It did not go quietly. Escalate.
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    log(f"killed {name} (it did not stop on SIGTERM)", "warn")
    return True


# ----------------------------------------------------------------- commands


def cmd_up(args) -> int:
    print()
    print("=" * 68)
    print("  Bringing the test environment up")
    print("=" * 68)

    state = read_state()
    problems: List[str] = []

    # 1. The ledger.
    log("checking the XRP Ledger devnet...")
    if ledger_ok():
        log("the devnet is already running and answering")
    else:
        log("the devnet is not answering; starting it with docker compose")
        compose = find_compose()
        if compose:
            result = subprocess.run(
                ["docker", "compose", "-f", str(compose), "up", "-d"],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                problems.append(
                    f"docker compose failed: {result.stderr.strip()[:300]}"
                )
            else:
                log("docker compose started the devnet")
        else:
            problems.append(
                "no docker compose file for the devnet was found; start it "
                "manually and re-run"
            )

    if not ledger_ok():
        problems.append(
            "the ledger did not answer server_info on " + RIPPLE_WS
        )
    else:
        wait_for("the XRP Ledger devnet", ledger_ok, timeout=60)

    # 2. The reference ERP.
    log("starting the reference ERP...")
    erp_pid = state.get("erp", {}).get("pid")
    if erp_pid and alive(erp_pid):
        log(f"the reference ERP is already running (pid {erp_pid})")
    else:
        erp_pid = start_service("erp", ERP_COMMAND)
        state.setdefault("erp", {})["pid"] = erp_pid
        state["erp"]["command"] = ERP_COMMAND

    erp_health = f"http://127.0.0.1:{ERP_PORT}/health"
    if not wait_for(
        "the reference ERP",
        lambda: http_ok(erp_health)[0],
        timeout=45,
    ):
        problems.append(f"the reference ERP never answered on {erp_health}")

    # 3. The signing service. It holds the key; the treasury service
    #    does not. Pointing the treasury at it is what makes the
    #    EXTERNAL signing path the live one, rather than the local
    #    seed.
    log("starting the signing service...")
    signer_pid = state.get("signer", {}).get("pid")
    if signer_pid and alive(signer_pid):
        log(f"the signing service is already running (pid {signer_pid})")
    else:
        signer_pid = start_service("signer", SIGNER_COMMAND)
        state.setdefault("signer", {})["pid"] = signer_pid
        state["signer"]["command"] = SIGNER_COMMAND

    signer_health = f"http://127.0.0.1:{SIGNER_PORT}/health"
    if not wait_for(
        "the signing service",
        lambda: http_ok(signer_health)[0],
        timeout=45,
    ):
        problems.append(f"the signing service never answered on {signer_health}")

    # 4. The treasury service.
    log("minting the API credentials...")
    ensure_credentials()
    log(f"  {CREDENTIAL_FILE} (mode 0600)")

    log("starting the treasury service...")
    service_pid = state.get("service", {}).get("pid")
    if service_pid and alive(service_pid):
        log(f"the treasury service is already running (pid {service_pid})")
    else:
        command = [
            item.format(host=args.host, erp_port=ERP_PORT)
            for item in SERVICE_COMMAND
        ]
        credentials = ensure_credentials()
        service_env = {
            "TREASURY_API_TOKENS": json.dumps(credentials),
            # The treasury service signs THROUGH the signing service. It
            # holds no key.
            "TREASURY_SIGNER_URL": f"http://127.0.0.1:{SIGNER_PORT}/sign",
            "TREASURY_SIGNER_TOKEN": SIGNER_TOKEN,
        }
        service_pid = start_service("service", command, env=service_env)
        state.setdefault("service", {})["pid"] = service_pid
        state["service"]["command"] = command

    service_health = f"http://{args.host}:{SERVICE_PORT}/api/health"
    if not wait_for(
        "the treasury service",
        lambda: http_ok(service_health)[0],
        timeout=60,
    ):
        problems.append(f"the treasury service never answered on {service_health}")

    state["host"] = args.host
    state["service_port"] = SERVICE_PORT
    state["erp_port"] = ERP_PORT
    write_state(state)

    print()
    if problems:
        print("  THE ENVIRONMENT IS NOT READY:")
        for problem in problems:
            print(f"    - {problem}")
        print()
        print("  Logs are in", LOG_DIR)
        return 1

    print("  THE ENVIRONMENT IS READY")
    print(f"    ledger    {RIPPLE_WS}")
    print(f"    ERP       http://127.0.0.1:{ERP_PORT}")
    print(f"    treasury  http://{args.host}:{SERVICE_PORT}")
    print()
    print("  Next: python3 dev_env.py test")
    print()
    return 0


def find_compose() -> Optional[pathlib.Path]:
    """Locate the xrpl-up compose file."""
    candidates = [
        HERE / "devnet" / "docker-compose.yml",
        HERE / ".dev-env" / "docker-compose.yml",
        HERE.parent / "XRPL_LOCAL_NODE" / "docker-compose.yml",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    # The xrpl-up project keeps its compose file under the home directory.
    for pattern in (
        "xrpl-up*/docker-compose.yml",
        "xrpl-up*/compose.yml",
        ".xrpl-up/docker-compose.yml",
    ):
        for match in pathlib.Path.home().glob(pattern):
            return match
    return None


def cmd_reset(args) -> int:
    """Tear the ledger down to a clean slate and bring it back.

    This destroys the devnet's ledger state: every account, trust line,
    credential, domain and transaction on it. It is a TEST environment
    reset, and it means the account seeds are regenerated - so anything
    the treasury service was configured with has to be rebuilt by
    prepare_beneficiaries.py afterwards.
    """
    print()
    print("=" * 68)
    print("  Resetting the ledger to a clean slate")
    print("=" * 68)

    compose = find_compose()
    if not compose:
        log("no docker compose file for the devnet was found", "error")
        return 1

    log(f"compose file: {compose}")

    # Stop the services first: they hold a websocket to the ledger.
    state = read_state()
    for name in ("service", "signer", "erp"):
        entry = state.get(name)
        if entry:
            stop_service(name, entry.get("pid", 0))

    log("removing the devnet containers and its ledger volume")
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose), "down", "-v"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        log(f"docker compose down failed: {result.stderr.strip()[:300]}", "error")
        return 1

    log("starting a fresh devnet")
    # A brand new Docker volume is owned by root, but rippled runs as the
    # unprivileged user xrpld (uid 999). It cannot create its own
    # database directory and exits with "Permission denied".
    #
    # The volume is then seeded from the snapshot the environment was
    # built with. The snapshot's ledger.db carries the right genesis
    # (matching validators.txt, so the node becomes a VALIDATOR and
    # leads consensus instead of observing) and the right ownership.
    #
    # Restoring the snapshot alone is not enough: the compose entrypoint
    # runs --load when ledger.db exists and --start otherwise, and a
    # half-cleared volume produces an "observing" node that answers
    # server_info but refuses every account with
    # InsufficientNetworkMode. So the ledger is cleared, the snapshot is
    # restored on top of it, and the container is recreated.
    # Only the FIRST node is seeded from a snapshot.
    #
    # A snapshot carries a genesis that matches validators.txt, so the
    # node becomes a VALIDATOR and leads consensus. The SECOND node
    # must NOT be seeded from its own snapshot: the two snapshots have
    # different genesis ledgers, so the nodes end up on different
    # networks, neither can reach consensus with the other, and the
    # ledger never advances past its first few ledgers.
    #
    # Instead the second node's database is left EMPTY so its entrypoint
    # runs --start and creates a genesis from the SAME validators file,
    # which produces the same genesis as the first node.
    for volume, snapshot in (
        ("xrpl-up-local-db", "node1-db.tar.gz"),
    ):
        archive = compose.parent / snapshot
        if not archive.exists():
            log(f"no snapshot for {volume} ({snapshot}); it will be empty")
            continue
        log(f"seeding {volume} from {snapshot}")
        seed = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{volume}:/db",
                "-v",
                f"{archive}:/seed.tar.gz:ro",
                "alpine",
                "sh",
                "-c",
                "cd /db && rm -rf ./* && tar xzf /seed.tar.gz && chown -R 999:999 /db",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if seed.returncode != 0:
            log(f"seeding {volume} failed: {seed.stderr.strip()[:200]}", "error")
            return 1

    # The peer node gets an empty database with the right ownership.
    peer_volume = "xrpl-up-local-peer-db"
    log(f"clearing {peer_volume} so it creates a matching genesis")
    cleared = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{peer_volume}:/db",
            "alpine",
            "sh",
            "-c",
            "cd /db && rm -rf ./* && chown 999:999 /db",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if cleared.returncode != 0:
        log(f"clearing {peer_volume} failed: {cleared.stderr.strip()[:200]}", "error")
        return 1

    result = subprocess.run(
        ["docker", "compose", "-f", str(compose), "up", "-d", "--force-recreate"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        log(f"docker compose up failed: {result.stderr.strip()[:300]}", "error")
        return 1

    if not wait_for("the fresh XRP Ledger devnet", ledger_ok, timeout=180):
        log("the fresh devnet never became ready", "error")
        return 1

    # The node has to be a VALIDATOR, not merely an observer, or it
    # refuses every account with InsufficientNetworkMode.
    if not wait_for(
        "the devnet to become a validator",
        lambda: ledger_is_validator(),
        timeout=120,
    ):
        log("the devnet came up but is not leading consensus", "error")
        return 1

    # And the ledger has to be ADVANCING. A node that is proposing but
    # cannot reach consensus leaves the ledger frozen at its first
    # ledgers, and the accounts stay unfunded.
    if not wait_for(
        "the ledger to start advancing",
        lambda: ledger_advancing(),
        timeout=120,
    ):
        log("the ledger never advanced past its first ledgers", "error")
        return 1

    print()
    print("  THE LEDGER IS CLEAN")
    print("  The account seeds have been regenerated. Run")
    print("      python3 prepare_beneficiaries.py")
    print("  before the settlement checks will pass.")
    print()
    return 0


def fund_accounts() -> bool:
    """Create and fund every devnet account from the faucet.

    A freshly created genesis contains no accounts. The faucet's own
    accounts file lists the addresses and seeds the environment uses, so
    each one is asked for. An account that already exists is not an
    error - the faucet tops it up.
    """
    accounts_file = pathlib.Path.home() / ".xrpl-up" / "local-accounts.json"
    if not accounts_file.exists():
        log(f"{accounts_file} does not exist", "error")
        return False

    raw = json.loads(accounts_file.read_text())
    if isinstance(raw, dict) and "accounts" in raw:
        raw = raw["accounts"]
    items = raw if isinstance(raw, list) else list(raw.values())

    funded = 0
    for item in items:
        address = item.get("address")
        if not address:
            continue
        try:
            request = urllib.request.Request(
                FAUCET_URL,
                data=json.dumps({"destination": address}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read().decode())
            funded += 1
            time.sleep(0.3)
        except Exception as exc:
            log(f"could not fund {address}: {exc!r}", "warn")

    log(f"funded {funded} of {len(items)} accounts")
    return funded > 0


def cmd_seed(args) -> int:
    """Build the ledger fixtures the treasury service needs.

    A clean ledger has no RLUSD, no treasury, no PermissionedDomain and
    no beneficiaries. This runs the setup scripts in the order they
    depend on each other, so the environment can be rebuilt from nothing.
    """
    print()
    print("=" * 68)
    print("  Seeding the ledger with the treasury's fixtures")
    print("=" * 68)

    # A clean genesis has no funded accounts at all. The faucet creates
    # and funds them; every later step depends on this one.
    log("funding the devnet accounts from the faucet...")
    if not fund_accounts():
        log("the faucet could not fund the accounts", "error")
        return 1

    steps = [
        # FIRST: the issuer's rippling default. Every trust line created
        # after this point can ripple. A line created before it carries a
        # stale issuer-side no-ripple bit that cannot be repaired.
        ("enable the issuer's rippling", HERE / "enable_issuer_rippling.py"),
        ("issue the currencies", HERE / "issue_currencies.py"),
        ("create the domain", HERE / "create_domain.py"),
        ("fund the treasury", HERE / "fund_treasury.py"),
        ("prepare the beneficiaries", HERE / "prepare_beneficiaries.py"),
    ]

    for label, script in steps:
        if not script.exists():
            log(f"SKIPPED {label}: {script.name} does not exist", "warn")
            continue
        log(f"running {script.name} ({label})...")
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            cwd=str(HERE),
            timeout=600,
        )
        if result.returncode != 0:
            log(f"FAILED {label}", "error")
            print(result.stdout[-3000:])
            print(result.stderr[-3000:])
            return 1
        log(f"done: {label}")

    print()
    print("  THE LEDGER IS SEEDED")
    print()
    return 0


def cmd_verify_all(args) -> int:
    """Run every layer of verification and report them together.

    There are four independent layers, and each catches things the
    others cannot:

      1. UNIT TESTS      the pure logic - policy, currency, planner,
                         signer, logging. Fast, no ledger, no network.
      2. THE API        the service over HTTP, asserting on RESPONSES.
                         Catches broken contracts and wrong status codes
                         that a unit test cannot see.
      3. THE HARNESS     verify.py - the service's own deep assertions
                         against the live ledger: the perimeter, the
                         tier decisions, the cross-currency construction,
                         the hash-chained log.
      4. THE SCALE       scale_test.py - throughput, the policy at
                         volume, memory stability, and the deployment
                         readiness checks.

    A layer that fails does not stop the others: the point is to see
    the whole picture, not just the first thing that broke.
    """
    print()
    print("=" * 68)
    print("  Verifying every layer")
    print("=" * 68)

    layers = []

    # 1. Unit tests.
    print()
    log("LAYER 1 - unit tests")
    unit = subprocess.run(
        [sys.executable, "-m", "pytest", str(HERE / "tests"), "-q"],
        capture_output=True,
        text=True,
        cwd=str(HERE),
        env={**os.environ, "PYTHONPATH": str(HERE / "src")},
    )
    tail = unit.stdout.strip().splitlines()[-1] if unit.stdout.strip() else ""
    layers.append(("unit tests", unit.returncode == 0, tail))
    print(f"    {'PASS' if unit.returncode == 0 else 'FAIL'}  {tail}")

    # 2. The API harness.
    print()
    log("LAYER 2 - the API harness")
    base = f"http://{args.host}:{SERVICE_PORT}"
    command = [sys.executable, str(HERE / "api_test.py"), "--base", base]
    if args.allow_move:
        command.append("--allow-move")
    api = subprocess.run(command, capture_output=True, text=True, cwd=str(HERE))
    summary = ""
    for line in api.stdout.splitlines():
        if "checks" in line and ("PASS" in line or "FAIL" in line):
            summary = line.strip()
    ok = api.returncode == 0
    layers.append(("the API harness", ok, summary or f"exit {api.returncode}"))
    print(f"    {'PASS' if ok else 'FAIL'}  {summary}")

    # 3. verify.py.
    print()
    log("LAYER 3 - the deep verification harness")
    verify_command = [sys.executable, str(HERE / "verify.py")]
    if args.allow_move:
        verify_command.append("--allow-move")
    verify = subprocess.run(
        verify_command, capture_output=True, text=True, cwd=str(HERE)
    )
    summary = ""
    for line in verify.stdout.splitlines():
        if "assertions passed" in line:
            summary = line.strip()
    ok = verify.returncode == 0
    layers.append(("the verification harness", ok, summary or f"exit {verify.returncode}"))
    print(f"    {'PASS' if ok else 'FAIL'}  {summary}")

    # 4. The scale harness.
    print()
    log("LAYER 4 - scale and deployment readiness")
    instructions = args.instructions
    scale = subprocess.run(
        [
            sys.executable,
            str(HERE / "scale_test.py"),
            "--instructions",
            str(instructions),
            "--out",
            str(HERE / "scale-results"),
        ],
        capture_output=True,
        text=True,
        cwd=str(HERE),
    )
    summary = ""
    for line in scale.stdout.splitlines():
        if "phases" in line and ("PASS" in line or "FAIL" in line):
            summary = line.strip()
    ok = scale.returncode == 0
    layers.append(("the scale harness", ok, summary or f"exit {scale.returncode}"))
    print(f"    {'PASS' if ok else 'FAIL'}  {summary}")

    print()
    print("=" * 68)
    print("  THE VERIFICATION SUMMARY")
    print("=" * 68)
    for name, ok, summary in layers:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<28} {summary}")
    passed = sum(1 for _, ok, _ in layers if ok)
    print()
    print(f"  {passed}/{len(layers)} layers pass")
    print()
    return 0 if passed == len(layers) else 1


def cmd_test(args) -> int:
    print()
    print("=" * 68)
    print("  Running the test suite")
    print("=" * 68)

    base = f"http://{args.host}:{SERVICE_PORT}"

    # The unit tests.
    print()
    log("running the unit tests...")
    unit = subprocess.run(
        [sys.executable, "-m", "pytest", str(HERE / "tests"), "-q"],
        capture_output=True,
        text=True,
        cwd=str(HERE),
        env={**os.environ, "PYTHONPATH": str(HERE / "src")},
    )
    unit_tail = unit.stdout.strip().splitlines()[-1] if unit.stdout.strip() else ""
    if unit.returncode == 0:
        log(f"unit tests passed: {unit_tail}")
    else:
        log(f"UNIT TESTS FAILED: {unit_tail}", "error")
        print(unit.stdout[-3000:])
        print(unit.stderr[-2000:])
        return 1

    # The API harness.
    print()
    log("running the API harness...")
    command = [sys.executable, str(HERE / "api_test.py"), "--base", base]
    if args.allow_move:
        command.append("--allow-move")
    api = subprocess.run(command, capture_output=True, text=True, cwd=str(HERE))
    print(api.stdout)
    if api.stderr.strip():
        print(api.stderr[-2000:])
    if api.returncode != 0:
        log("the API harness FAILED", "error")
        return 1

    log("every suite passed")
    return 0


def cmd_down(args) -> int:
    print()
    print("=" * 68)
    print("  Tearing the environment down")
    print("=" * 68)

    state = read_state()
    for name in ("service", "signer", "erp"):
        entry = state.get(name)
        if not entry:
            log(f"{name} was not recorded; nothing to stop")
            continue
        stop_service(name, entry.get("pid", 0))

    if PID_FILE.exists():
        PID_FILE.unlink()
    log("state cleared")
    print()
    return 0


def cmd_status(args) -> int:
    print()
    print("=" * 68)
    print("  Environment status")
    print("=" * 68)

    state = read_state()

    print()
    print("  components")
    print(
        f"    ledger      {'UP  ' if ledger_ok() else 'DOWN'}  {RIPPLE_WS}"
    )
    ok, body = http_ok(f"http://127.0.0.1:{ERP_PORT}/health")
    print(
        f"    ERP         {'UP  ' if ok else 'DOWN'}  "
        f"http://127.0.0.1:{ERP_PORT}"
    )
    ok, body = http_ok(
        f"http://{args.host}:{SERVICE_PORT}/api/health"
    )
    print(
        f"    treasury    {'UP  ' if ok else 'DOWN'}  "
        f"http://{args.host}:{SERVICE_PORT}"
    )

    print()
    print("  processes started by this script")
    for name in ("erp", "signer", "service"):
        entry = state.get(name)
        if not entry:
            print(f"    {name:<10} not recorded")
            continue
        pid = entry.get("pid", 0)
        print(
            f"    {name:<10} pid {pid:<8} "
            f"{'running' if alive(pid) else 'not running'}"
        )

    if ok and body:
        print()
        print("  treasury health")
        for key in sorted(body)[:10]:
            print(f"    {key}: {body[key]}")
    print()
    return 0


def cmd_fresh(args) -> int:
    """Build the environment from nothing and test it.

    This is the whole thing, end to end:

        reset   wipe the ledger to a clean genesis
        seed    fund the accounts, issue the currencies, create the
                domain, fund the treasury, enrol the members
        up      start the ERP and the treasury service
        test    run the unit tests and the API harness

    A failure at any stage stops the sequence, so a green result here
    means the environment was BUILT, not that it was inherited from a
    previous run.
    """
    print()
    print("=" * 68)
    print("  Building the environment from nothing")
    print("=" * 68)

    started = time.time()
    for stage in (cmd_reset, cmd_seed, cmd_up, cmd_test):
        result = stage(args)
        if result != 0:
            log(f"{stage.__name__} failed; stopping", "error")
            return result

    print()
    print("=" * 68)
    print(f"  BUILT AND TESTED IN {time.time() - started:.0f}s")
    print("=" * 68)
    print()
    return 0


def cmd_cycle(args) -> int:
    result = cmd_up(args)
    if result != 0:
        return result
    try:
        return cmd_test(args)
    finally:
        cmd_down(args)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-command test environment for the treasury service"
    )
    parser.add_argument(
        "command",
        choices=[
            "up", "test", "down", "status", "cycle", "reset", "seed", "fresh",
            "verify-all",
        ],
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="the address the treasury service binds to (default 127.0.0.1)",
    )
    parser.add_argument(
        "--allow-move",
        action="store_true",
        help="let the API harness submit a real transaction to the devnet",
    )
    parser.add_argument(
        "--instructions",
        type=int,
        default=100_000,
        help="how many instructions the scale layer plans (default 100000)",
    )
    args = parser.parse_args()

    handlers = {
        "up": cmd_up,
        "test": cmd_test,
        "down": cmd_down,
        "status": cmd_status,
        "cycle": cmd_cycle,
        "reset": cmd_reset,
        "seed": cmd_seed,
        "fresh": cmd_fresh,
        "verify-all": cmd_verify_all,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
