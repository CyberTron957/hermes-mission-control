#!/usr/bin/env python3
import time
import subprocess
import httpx
import os
import sys

def main():
    print("=" * 60)
    print("  Swarm Race Conditions & 3-Agent Integration Tests")
    print("=" * 60)

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
        # 1. Wait for the FastAPI server to start
        print("\n[Test Setup] Waiting for server health endpoint to be ok...")
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

        # Let the initial trigger execute first (it has a 3.0s delay in test_swarm.py)
        print("Waiting for startup initial trigger to enqueue task...")
        time.sleep(5)

        # =====================================================================
        # Race Condition 1: Queue a task while agent is in 'asking_human' state.
        # =====================================================================
        print("\n--- TEST case 1: Queueing a task during 'asking_human' state ---")
        
        # Wait for Editor to be in asking_human state
        for _ in range(30):
            r = httpx.get("http://127.0.0.1:8000/agent/editor/status")
            status = r.json().get("state")
            print(f"Editor state: {status}")
            if status == "asking_human":
                break
            time.sleep(2)
        else:
            raise RuntimeError("Editor did not enter asking_human state.")

        # Queue a second task now while the Editor is blocked waiting for human
        print("Queueing second task to Editor while in asking_human state...")
        r = httpx.post(
            "http://127.0.0.1:8000/agent/editor/task",
            json={
                "from_agent": "test_script",
                "payload": "This is a secondary follow-up task. Just answer with 'Received secondary task' and stop."
            }
        )
        print("Task ingestion response:", r.json())

        # =====================================================================
        # Race Condition 2 & 3: Duplicate and invalid human responses.
        # =====================================================================
        print("\n--- TEST case 2: Duplicate / Invalid human responses ---")
        
        # Send first valid response
        print("Sending first valid response 'Mango'...")
        r1 = httpx.post("http://127.0.0.1:8000/agent/editor/human_response", json={"response": "Mango"})
        print("R1 response:", r1.status_code, r1.json())
        assert r1.status_code == 200, "First response should succeed"

        # Send duplicate response immediately
        print("Sending duplicate response immediately...")
        r2 = httpx.post("http://127.0.0.1:8000/agent/editor/human_response", json={"response": "Apple"})
        print("R2 response (expect error 400):", r2.status_code, r2.json())
        assert r2.status_code == 400, "Duplicate response should fail with 400"

        # Wait for Editor to process task 1 and task 2
        print("Waiting for Editor to finish both tasks...")
        for _ in range(40):
            r = httpx.get("http://127.0.0.1:8000/agent/editor/status")
            status = r.json().get("state")
            print(f"Editor state: {status}")
            if status == "idle":
                # Let's make sure it doesn't immediately start again if it has tasks
                time.sleep(3)
                r = httpx.get("http://127.0.0.1:8000/agent/editor/status")
                if r.json().get("state") == "idle":
                    print("Editor is idle and completed all tasks!")
                    break
            time.sleep(2)
        else:
            raise RuntimeError("Editor did not return to idle state after processing tasks.")

        # Try to send a human response when agent is NOT asking human
        print("\n--- TEST case 3: Response when agent is idle (not asking) ---")
        r3 = httpx.post("http://127.0.0.1:8000/agent/editor/human_response", json={"response": "Banana"})
        print("R3 response (expect error 400):", r3.status_code, r3.json())
        assert r3.status_code == 400, "Sending response to idle agent should fail with 400"

        # =====================================================================
        # 3-Agent Swarm Test: Editor -> Researcher -> Reviewer -> Editor
        # =====================================================================
        print("\n--- TEST case 4: 3-Agent Chain (Editor -> Researcher -> Reviewer -> Editor) ---")
        
        # Inject the task chain starting at editor
        payload = (
            "We have a 3-agent team: editor, researcher, and reviewer.\n"
            "Please send a message to the researcher using send_peer_message saying: 'Please reverse the word XYZ and send the result to the reviewer'.\n"
            "After you send the message, stop."
        )
        print("Injecting task chain into Editor...")
        r = httpx.post("http://127.0.0.1:8000/agent/editor/task", json={"from_agent": "test_script", "payload": payload})
        print("Editor chain task queued:", r.json())

        # Wait to let the swarm run the chain.
        # Editor will send a message to Researcher.
        # In Researcher's sweep, it will read it, write a summary/result, and send it to Reviewer.
        # In Reviewer's sweep, it will approve it and send it to Editor.
        # Editor will receive the approval and stop.
        print("Waiting for agents to complete the 3-agent collaboration pipeline...")
        for i in range(25):
            for agent in ["editor", "researcher", "reviewer"]:
                try:
                    r = httpx.get(f"http://127.0.0.1:8000/agent/{agent}/status").json()
                    if r.get("state") == "asking_human":
                        print(f"[{agent}] Detected asking_human state. Responding...")
                        httpx.post(f"http://127.0.0.1:8000/agent/{agent}/human_response", json={"response": "XYZ refers literally to the letters XYZ."})
                except Exception as e:
                    print(f"Error checking status for {agent}: {e}")

            e_status = httpx.get("http://127.0.0.1:8000/agent/editor/status").json().get("state")
            res_status = httpx.get("http://127.0.0.1:8000/agent/researcher/status").json().get("state")
            rev_status = httpx.get("http://127.0.0.1:8000/agent/reviewer/status").json().get("state")
            print(f"Status - Editor: {e_status} | Researcher: {res_status} | Reviewer: {rev_status}")
            time.sleep(3)

        print("\nTests completed. Closing server.")

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
