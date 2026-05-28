#!/usr/bin/env python3
import time
import subprocess
import httpx
import os
import sys
from pathlib import Path

def main():
    print("=" * 60)
    print("  Testing Dynamic Agents & Custom Soul Modifications")
    print("=" * 60)

    workspace_root = Path("./swarm_test_environment")
    config_file = workspace_root / "agents_config.json"
    
    # Remove config file if it exists to ensure a clean start
    if config_file.exists():
        config_file.unlink()
        print("Cleaned up old agents_config.json.")

    env = {
        **os.environ,
        "PYTHONPATH": "/Users/pradhyun/.hermes/hermes-agent"
    }
    server_process = subprocess.Popen(
        [sys.executable, "-u", "test_swarm.py"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )

    try:
        # 1. Wait for server to start
        print("\n[Setup] Waiting for server to be healthy...")
        for _ in range(30):
            try:
                r = httpx.get("http://127.0.0.1:8000/health")
                if r.status_code == 200:
                    print("Server is UP:", r.json())
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            print("Error: Server failed to start.")
            server_process.terminate()
            return

        # 2. Get active agents list
        r = httpx.get("http://127.0.0.1:8000/agents")
        print("\nInitial agents list from server:", r.json().keys())

        # 3. Add a new custom agent 'translator' with a French soul
        print("\nRegistering new custom agent 'translator' with a French soul...")
        translator_soul = (
            "You are the Translator Agent.\n"
            "When you receive any task, translate the input text to French.\n"
            "Then, write the French translation to a file named 'french.txt' inside your workspace using standard python/file tools or writing skills, and stop.\n"
            "Do NOT call other agents or ask human, just translate and save."
        )
        payload = {
            "agent_name": "translator",
            "name": "Translator Agent",
            "session_id": "translator-session-v1",
            "workspace": "translator",
            "port": 8103,
            "peer_name": "editor",
            "peer_port": 8100,
            "soul": translator_soul
        }
        r = httpx.post("http://127.0.0.1:8000/agent", json=payload)
        print("Registration response:", r.status_code, r.json())
        assert r.status_code == 200

        # Verify it shows up in active agents
        r = httpx.get("http://127.0.0.1:8000/agents")
        print("Updated agents list from server:", r.json().keys())
        assert "translator" in r.json()

        # 4. Ingest a task into translator
        print("\nSending English text to translator...")
        r = httpx.post(
            "http://127.0.0.1:8000/agent/translator/task",
            json={
                "from_agent": "human_manager",
                "payload": "Hello, how are you doing today?"
            }
        )
        print("Task queued response:", r.json())

        # Wait and poll for french.txt to be written
        french_txt_path = workspace_root / "translator" / "french.txt"
        print(f"Waiting for {french_txt_path} to be created by the agent...")
        for _ in range(30):
            if french_txt_path.exists():
                content = french_txt_path.read_text(encoding="utf-8").strip()
                print(f"Success! french.txt content: '{content}'")
                break
            time.sleep(2)
        else:
            raise RuntimeError("french.txt was not created in time.")

        # 5. Update translator's soul to do Spanish translation
        print("\nUpdating translator soul to Spanish translation...")
        spanish_soul = (
            "You are the Translator Agent.\n"
            "When you receive any task, translate the input text to Spanish.\n"
            "Then, write the Spanish translation to a file named 'spanish.txt' inside your workspace, and stop.\n"
            "Do NOT call other agents or ask human, just translate and save."
        )
        r = httpx.post("http://127.0.0.1:8000/agent/translator/soul", json={"soul": spanish_soul})
        print("Soul update response:", r.status_code, r.json())
        assert r.status_code == 200

        # 6. Ingest a task into translator with the new soul
        print("\nSending English text to translator to check Spanish translation...")
        r = httpx.post(
            "http://127.0.0.1:8000/agent/translator/task",
            json={
                "from_agent": "human_manager",
                "payload": "Good morning my friend."
            }
        )
        print("Second task queued response:", r.json())

        # Wait and poll for spanish.txt to be written
        spanish_txt_path = workspace_root / "translator" / "spanish.txt"
        print(f"Waiting for {spanish_txt_path} to be created by the agent...")
        for _ in range(30):
            if spanish_txt_path.exists():
                content = spanish_txt_path.read_text(encoding="utf-8").strip()
                print(f"Success! spanish.txt content: '{content}'")
                break
            time.sleep(2)
        else:
            raise RuntimeError("spanish.txt was not created in time.")

        print("\nAll dynamic agent tests completed successfully!")

    finally:
        print("\nTerminating server...")
        server_process.terminate()
        try:
            stdout, _ = server_process.communicate(timeout=10)
            print("--- Server Logs ---")
            print(stdout)
            print("-------------------")
        except subprocess.TimeoutExpired:
            server_process.kill()
            stdout, _ = server_process.communicate()
            print("--- Server Logs (Killed) ---")
            print(stdout)
            print("-------------------")

if __name__ == "__main__":
    main()
