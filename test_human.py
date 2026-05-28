#!/usr/bin/env python3
import time
import subprocess
import httpx
import os
import sys

def main():
    print("Starting swarm server...")
    # Run test_swarm.py in the background
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
        # Wait for the FastAPI server to start
        print("Waiting for server health endpoint to be ok...")
        for _ in range(30):
            try:
                r = httpx.get("http://127.0.0.1:8000/health")
                if r.status_code == 200:
                    print("Server is UP and healthy:", r.json())
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            print("Error: Server failed to start.")
            server_process.terminate()
            return

        # Poll status of editor agent
        print("Polling editor agent status to transition to asking_human...")
        asking_human_detected = False
        for _ in range(60):
            # Check server output lines
            try:
                r = httpx.get("http://127.0.0.1:8000/agent/editor/status")
                status = r.json()
                print(f"Current editor status: {status}")
                if status.get("state") == "asking_human":
                    asking_human_detected = True
                    break
            except Exception as e:
                print(f"Status check failed: {e}")
            time.sleep(2)

        if not asking_human_detected:
            print("Error: Editor agent did not enter asking_human state.")
            # Print recent output from server
            print("Recent server logs:")
            server_process.terminate()
            stdout, _ = server_process.communicate(timeout=5)
            print(stdout)
            return

        print("Editor agent is asking human. Sending response 'Mango'...")
        r = httpx.post("http://127.0.0.1:8000/agent/editor/human_response", json={"response": "Mango"})
        print("Response submission response:", r.status_code, r.json())

        # Wait for agent to finish and become idle again
        print("Waiting for agent to complete task...")
        for _ in range(30):
            try:
                r = httpx.get("http://127.0.0.1:8000/agent/editor/status")
                status = r.json()
                print(f"Current editor status: {status}")
                if status.get("state") == "idle":
                    print("Editor is idle again.")
                    break
            except Exception:
                pass
            time.sleep(2)

        # Allow time for logging final completion
        time.sleep(3)

    finally:
        print("Terminating server...")
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
