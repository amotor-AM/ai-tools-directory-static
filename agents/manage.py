#!/usr/bin/env python3
"""Agent Manager for ARIA — Route, spawn, and manage specialized sub-agents.

The central nervous system of ARIA's agent architecture. Handles routing messages
to the right agent, preparing spawn prompts, model selection, and agent creation.

Usage:
  # Route a message to the right agent
  manage.py route "generate a portrait of Jinx"

  # Prepare and spawn an agent for a task
  manage.py spawn imageagent --task "generate a portrait of Jinx in golden hour lighting"

  # List all registered agents
  manage.py list

  # Show agent details
  manage.py show imageagent

  # Create a new agent
  manage.py create myagent --name "My Agent" --model "anthropic/claude-sonnet-4-5" \
    --description "Does cool things" --capabilities "cap1,cap2" --skills "aria-dev"

  # Generate agent builder prompt (for creating agents from descriptions)
  manage.py build --description "I need an agent that can manage Kubernetes deployments"

  # Check running agent sessions
  manage.py status

  # Record an agent's task result
  manage.py result <task_id> --status success --summary "Generated 5 images"
"""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

AGENTS_DIR = Path("/home/alex/.openclaw/workspace/agents")
REGISTRY_PATH = AGENTS_DIR / "registry.json"
SKILLS_DIR = Path("/home/alex/.openclaw/workspace/skills")
SPAWN_DIR = Path("/tmp/aria-agents")
TASKS_DIR = AGENTS_DIR / "tasks"
TEMPLATES_DIR = Path("/home/alex/.openclaw/workspace/templates")


def load_registry():
    """Load the agent registry."""
    if not REGISTRY_PATH.exists():
        return {"version": 1, "agents": {}}
    with open(REGISTRY_PATH) as f:
        return json.load(f)


def save_registry(registry):
    """Save the agent registry."""
    with open(REGISTRY_PATH, "w") as f:
        json.dump(registry, f, indent=2)


def short_id():
    return uuid.uuid4().hex[:8]


def now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ─── ROUTE ──────────────────────────────────────────────────────────────

def route_message(args):
    """Determine which agent should handle a message/task."""
    message = args.message.strip()
    registry = load_registry()
    agents = registry.get("agents", {})

    if not agents:
        print("ERROR: No agents registered. Run manage.py with a populated registry.json.")
        sys.exit(1)

    # Strategy 1: Slash command match
    if message.startswith("/"):
        cmd = message.split()[0].lower().lstrip("/")
        for slug, agent in agents.items():
            agent_cmd = agent.get("command", "").lstrip("/").lower()
            if agent_cmd == cmd and agent.get("active", True):
                task_text = message[len(message.split()[0]):].strip() or message
                _print_route(slug, agent, "slash_command", 1.0, task_text)
                return

    # Strategy 2: Trigger word matching (highest priority for keyword routing)
    message_lower = message.lower()
    message_words = set(message_lower.split())
    scores = {}

    for slug, agent in agents.items():
        if not agent.get("active", True):
            continue

        score = 0.0

        # Trigger words — strong signals
        for trigger in agent.get("triggers", []):
            trigger_lower = trigger.lower()
            if trigger_lower in message_lower:
                # Multi-word triggers get higher weight
                word_count = len(trigger_lower.split())
                score += 3.0 * word_count

        # Capability matching
        for cap in agent.get("capabilities", []):
            cap_words = set(cap.lower().replace("_", " ").split())
            overlap = message_words & cap_words
            score += len(overlap) * 2.0

        # Description word matching
        desc_words = set(agent.get("description", "").lower().split())
        # Filter common words
        stop_words = {"a", "an", "the", "and", "or", "for", "to", "in", "on", "with", "via", "of", "is"}
        desc_words -= stop_words
        overlap = message_words & desc_words
        score += len(overlap) * 0.5

        # Agent name matching
        name_words = set(agent.get("name", "").lower().split())
        name_words -= stop_words
        overlap = message_words & name_words
        score += len(overlap) * 2.0

        if score > 0:
            scores[slug] = score

    if scores:
        best_slug = max(scores, key=scores.get)
        best_score = scores[best_slug]
        # Normalize confidence to 0-1 range (rough heuristic)
        confidence = min(best_score / 10.0, 1.0)
        task_text = message
        _print_route(best_slug, agents[best_slug], "keyword_match", confidence, task_text)
        return

    # Strategy 3: No match — suggest using general orchestrator or building an agent
    print("ROUTE: none")
    print("  No agent matched this task.")
    print("  OPTIONS:")
    print("    1. Handle it yourself (orchestrator mode)")
    print("    2. Build a new agent: manage.py build --description \"...\"")
    print(f"  MESSAGE: {message}")


def _print_route(slug, agent, method, confidence, task_text):
    """Print routing result in structured format."""
    model = _select_model(agent, task_text)
    print(f"ROUTE: {slug}")
    print(f"  name: {agent['name']}")
    print(f"  model: {model}")
    print(f"  command: {agent.get('command', 'none')}")
    print(f"  method: {method}")
    print(f"  confidence: {confidence:.2f}")
    print(f"  spawn_method: {agent.get('spawn_method', 'session')}")
    print(f"  task: {task_text}")


# ─── MODEL SELECTION ────────────────────────────────────────────────────

def _select_model(agent, task_text=""):
    """Select the right model based on agent config and task content."""
    task_lower = task_text.lower() if task_text else ""

    # Check for NSFW/uncensored signals
    nsfw_signals = ["nsfw", "explicit", "nude", "naked", "porn", "sex", "erotic",
                    "uncensored", "adult", "xxx"]
    is_nsfw = any(signal in task_lower for signal in nsfw_signals)

    if is_nsfw and agent.get("use_uncensored"):
        return agent.get("model_uncensored", "ollama/huihui_ai/glm-4.7-flash-abliterated")

    return agent.get("model_primary", "anthropic/claude-sonnet-4-5")


# ─── SPAWN ──────────────────────────────────────────────────────────────

def spawn_agent(args):
    """Prepare a spawn prompt for an agent and write it to a file."""
    registry = load_registry()
    agents = registry.get("agents", {})

    if args.slug not in agents:
        print(f"ERROR: Agent '{args.slug}' not found. Available: {', '.join(agents.keys())}")
        sys.exit(1)

    agent = agents[args.slug]
    task_text = args.task
    task_id = short_id()

    # Select model
    if args.model:
        model = args.model
    elif args.uncensored and agent.get("use_uncensored"):
        model = agent.get("model_uncensored", "ollama/huihui_ai/glm-4.7-flash-abliterated")
    else:
        model = _select_model(agent, task_text)

    # Read AGENT.md if it exists
    agent_md_path = AGENTS_DIR / args.slug / "AGENT.md"
    agent_md = ""
    if agent_md_path.exists():
        agent_md = agent_md_path.read_text()
    else:
        # Fallback to personality from registry
        agent_md = f"# {agent['name']}\n\n{agent.get('personality', 'You are a specialized agent.')}\n"

    # Read referenced SKILL.md files
    skill_content = ""
    for skill_name in agent.get("skills", []):
        skill_path = SKILLS_DIR / skill_name / "SKILL.md"
        if skill_path.exists():
            content = skill_path.read_text()
            # Strip YAML frontmatter
            if content.startswith("---"):
                end = content.find("---", 3)
                if end != -1:
                    content = content[end + 3:].strip()
            skill_content += f"\n\n---\n## Skill Reference: {skill_name}\n\n{content}"

    # Try to load template, fall back to inline
    template_path = TEMPLATES_DIR / "spawn_prompt.md"
    if template_path.exists():
        template = template_path.read_text()
        prompt = template.replace("{{agent_name}}", agent["name"])
        prompt = prompt.replace("{{agent_personality}}", agent_md)
        prompt = prompt.replace("{{skill_references}}", skill_content)
        prompt = prompt.replace("{{task_description}}", task_text)
        prompt = prompt.replace("{{task_id}}", task_id)
        prompt = prompt.replace("{{agent_slug}}", args.slug)
    else:
        # Fallback to inline prompt (original behavior)
        prompt = f"""{agent_md}

{skill_content}

---

## YOUR TASK

{task_text}

---

## EXECUTION RULES

1. **Use absolute paths.** All file paths must start with `/home/alex/`
2. **Use exec with host="gateway".** Example: `exec: {{ "command": "...", "host": "gateway" }}`
3. **Work autonomously.** Do NOT ask for user input. Make the best decision you can.
4. **Try 3 approaches before failing.** If something breaks, try a different method.
5. **Record your work.** Update task state if using the task manager.

## OUTPUT FORMAT

When you complete this task, output your result in this exact format:

```
AGENT_RESULT:
{{
  "task_id": "{task_id}",
  "agent": "{args.slug}",
  "status": "success or error",
  "summary": "what you did (1-2 sentences)",
  "output_path": "/path/to/output/if/applicable",
  "details": "any additional details",
  "learned": "key insight from this task (optional)"
}}
```
"""

    # Write prompt to file
    SPAWN_DIR.mkdir(parents=True, exist_ok=True)
    prompt_file = SPAWN_DIR / f"spawn_{args.slug}_{task_id}.md"
    prompt_file.write_text(prompt)

    # Save task record
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    mission_id = getattr(args, "mission_id", None)
    task_record = {
        "task_id": task_id,
        "agent": args.slug,
        "model": model,
        "task": task_text,
        "status": "SPAWNED",
        "spawned_at": now_iso(),
        "prompt_file": str(prompt_file),
        "mission_id": mission_id,
    }
    task_file = TASKS_DIR / f"agent_task_{task_id}.json"
    with open(task_file, "w") as f:
        json.dump(task_record, f, indent=2)

    # Output spawn instructions
    spawn_method = agent.get("spawn_method", "session")

    print(f"SPAWN_READY:")
    print(f"  agent: {args.slug}")
    print(f"  name: {agent['name']}")
    print(f"  model: {model}")
    print(f"  task_id: {task_id}")
    print(f"  prompt_file: {prompt_file}")
    print(f"  task_record: {task_file}")
    print()

    if spawn_method == "session":
        print(f"SPAWN_COMMAND:")
        print(f'  Read the prompt file, then spawn a session:')
        print(f'  read: {{ "path": "{prompt_file}" }}')
        print(f'  Then use the prompt content with sessions_spawn or:')
        print(f'  exec: {{ "command": "/home/alex/.openclaw/workspace/skills/aria-dev/scripts/spawn-dev-session.sh {prompt_file} --model {model.split("/")[-1]}", "host": "gateway", "timeout": {agent.get("timeout_seconds", 600)} }}')
    elif spawn_method == "llm-task":
        print(f"SPAWN_COMMAND:")
        print(f'  llm-task: {{ "prompt": "<contents of {prompt_file}>", "model": "{model}" }}')
    else:
        print(f"SPAWN_COMMAND:")
        print(f'  Use sessions_spawn with the prompt file content and model: {model}')

    print()
    print(f"MONITOR:")
    print(f'  process: {{ "action": "list" }}')
    print(f'  Check for task_id: {task_id}')


# ─── LIST ───────────────────────────────────────────────────────────────

def list_agents(args):
    """List all registered agents."""
    registry = load_registry()
    agents = registry.get("agents", {})

    if args.json:
        print(json.dumps(agents, indent=2))
        return

    if not agents:
        print("No agents registered.")
        return

    print(f"{'COMMAND':<12} {'AGENT':<18} {'MODEL':<35} {'CAPABILITIES'}")
    print("-" * 100)
    for slug, agent in sorted(agents.items()):
        if not agent.get("active", True):
            continue
        cmd = agent.get("command", "")
        name = agent.get("name", slug)
        model = agent.get("model_primary", "?")
        # Shorten model name
        model_short = model.split("/")[-1][:30]
        caps = ", ".join(agent.get("capabilities", [])[:4])
        uncensored = " [NSFW]" if agent.get("use_uncensored") else ""
        print(f"{cmd:<12} {name:<18} {model_short:<35} {caps}{uncensored}")


# ─── SHOW ───────────────────────────────────────────────────────────────

def show_agent(args):
    """Show full agent details."""
    registry = load_registry()
    agents = registry.get("agents", {})

    if args.slug not in agents:
        print(f"ERROR: Agent '{args.slug}' not found.")
        sys.exit(1)

    agent = agents[args.slug]
    print(json.dumps(agent, indent=2))

    # Check for AGENT.md
    agent_md = AGENTS_DIR / args.slug / "AGENT.md"
    if agent_md.exists():
        print(f"\nAGENT.md: {agent_md}")
    else:
        print(f"\nAGENT.md: (not found — using registry personality)")

    # Check for tasks
    task_count = len(list(TASKS_DIR.glob(f"agent_task_*.json"))) if TASKS_DIR.exists() else 0
    print(f"Tasks: {task_count}")


# ─── CREATE ─────────────────────────────────────────────────────────────

def create_agent(args):
    """Create a new agent — directory, AGENT.md, and registry entry."""
    registry = load_registry()

    if args.slug in registry.get("agents", {}):
        print(f"ERROR: Agent '{args.slug}' already exists.")
        sys.exit(1)

    # Create directory
    agent_dir = AGENTS_DIR / args.slug
    agent_dir.mkdir(parents=True, exist_ok=True)

    # Parse capabilities and skills
    capabilities = [c.strip() for c in args.capabilities.split(",")] if args.capabilities else []
    skills = [s.strip() for s in args.skills.split(",")] if args.skills else []
    triggers = [t.strip() for t in args.triggers.split(",")] if args.triggers else []

    # Create registry entry
    agent_entry = {
        "name": args.name,
        "slug": args.slug,
        "command": args.command or f"/{args.slug}",
        "description": args.description,
        "personality": args.personality or f"You are the {args.name}, a specialized agent in ARIA's fleet.",
        "model_primary": args.model,
        "model_fallback": args.fallback or None,
        "model_uncensored": args.uncensored_model or "ollama/huihui_ai/glm-4.7-flash-abliterated",
        "use_uncensored": args.use_uncensored or False,
        "capabilities": capabilities,
        "skills": skills,
        "triggers": triggers,
        "spawn_method": args.spawn_method or "session",
        "max_concurrent": args.max_concurrent or 1,
        "timeout_seconds": args.timeout or 600,
        "active": True,
        "created_at": now_iso(),
        "created_by": args.created_by or "manual",
    }

    registry.setdefault("agents", {})[args.slug] = agent_entry
    save_registry(registry)

    # Create AGENT.md
    agent_md_content = f"""# {args.name}

## Identity
{args.personality or f"You are the {args.name}, a specialized agent in ARIA's fleet."}

## Capabilities
{chr(10).join(f"- {c}" for c in capabilities) or "- (define capabilities)"}

## Skills Reference
{chr(10).join(f"- Read: /home/alex/.openclaw/workspace/skills/{s}/SKILL.md" for s in skills) or "- (no skills referenced)"}

## Autonomy Directive
When you hit a blocker:
1. Self-diagnose — read the full error, check logs, try the obvious fix
2. Alternative approach — try a completely different method
3. Research — web search the error, read docs
4. Report failure with full context (what you tried, what failed, why)

You NEVER ask for user input. You work autonomously and report results.

## Output Format
Always end your work with:
```
AGENT_RESULT:
{{
  "status": "success" | "error",
  "summary": "what you did",
  "output_path": "/path/to/output" (if applicable),
  "learned": "key insight (optional)"
}}
```
"""
    agent_md_path = agent_dir / "AGENT.md"
    agent_md_path.write_text(agent_md_content)

    # Auto-update AGENTS.md command table so Aria sees the new agent
    _update_agents_md(agent_entry)

    # Auto-create SKILL.md so the gateway registers it as a Telegram slash command
    _create_skill_md(agent_entry, capabilities, skills)

    print(f"CREATED: {args.slug}")
    print(f"  Name: {args.name}")
    print(f"  Command: {agent_entry['command']}")
    print(f"  Model: {args.model}")
    print(f"  Directory: {agent_dir}")
    print(f"  AGENT.md: {agent_md_path}")
    print(f"  Capabilities: {capabilities}")
    print(f"  Skills: {skills}")

    # Auto-reset Aria's session and restart gateway so everything is live
    _reset_aria_session()
    _restart_gateway()


def _update_agents_md(agent_entry):
    """Append new agent to the command table in AGENTS.md."""
    agents_md = Path("/home/alex/.openclaw/workspace/AGENTS.md")
    if not agents_md.exists():
        return

    content = agents_md.read_text()
    command = agent_entry["command"]
    name = agent_entry["name"]
    desc = agent_entry["description"]
    # Truncate description for table
    short_desc = desc[:60] + "..." if len(desc) > 60 else desc

    # Find the last row of the command table (line starting with "| `/")
    lines = content.split("\n")
    insert_idx = None
    for i, line in enumerate(lines):
        if line.startswith("| `/"):
            insert_idx = i

    if insert_idx is not None:
        new_row = f"| `{command}` | {name} | {short_desc} |"
        # Check if already present
        if command not in content:
            lines.insert(insert_idx + 1, new_row)
            agents_md.write_text("\n".join(lines))
            print(f"  Updated AGENTS.md with {command}")
    else:
        print(f"  WARNING: Could not find command table in AGENTS.md")


def _reset_aria_session():
    """Archive Aria's current session to force skill re-snapshot on next message."""
    import glob
    import shutil
    from datetime import datetime

    sessions_dir = Path("/home/alex/.openclaw/agents/aria/sessions")
    sessions_json = sessions_dir.parent / "sessions.json" if sessions_dir.exists() else None

    # Find active session files (non-archived .jsonl)
    if not sessions_dir.exists():
        print("  Session reset: sessions dir not found, skipping")
        return

    active_sessions = list(sessions_dir.glob("*.jsonl"))
    active_sessions = [s for s in active_sessions if ".archived" not in s.name and ".deleted" not in s.name]

    if not active_sessions:
        print("  Session reset: no active sessions found")
        return

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    for session_file in active_sessions:
        archived_name = f"{session_file.stem}.jsonl.archived.{timestamp}"
        archived_path = session_file.parent / archived_name
        session_file.rename(archived_path)
        print(f"  Archived session: {session_file.name} -> {archived_name}")

    # Clear session from sessions.json if it exists
    if sessions_json and sessions_json.exists():
        try:
            with open(sessions_json) as f:
                sdata = json.load(f)
            # Remove entries for archived sessions
            for session_file in active_sessions:
                sid = session_file.stem
                if sid in sdata:
                    del sdata[sid]
            with open(sessions_json, "w") as f:
                json.dump(sdata, f, indent=2)
            print("  Cleared archived sessions from sessions.json")
        except Exception as e:
            print(f"  WARNING: Could not update sessions.json: {e}")

    print("  Session reset complete — Aria will re-snapshot skills on next message")


def _create_skill_md(agent_entry, capabilities, skills):
    """Create a SKILL.md in the skills directory so the gateway registers a Telegram slash command."""
    slug = agent_entry["slug"]
    # Skill dir name uses dashes (gateway convention)
    skill_dir_name = slug.replace("_", "-")
    skill_dir = SKILLS_DIR / skill_dir_name
    skill_md_path = skill_dir / "SKILL.md"

    if skill_md_path.exists():
        print(f"  SKILL.md already exists: {skill_md_path}")
        return

    skill_dir.mkdir(parents=True, exist_ok=True)

    name = agent_entry["name"]
    description = agent_entry["description"]
    model = agent_entry.get("model_uncensored") or agent_entry.get("model_primary", "")

    caps_list = "\n".join(f"- {c}" for c in capabilities) if capabilities else "- (see agent directory)"
    skills_list = "\n".join(
        f"- `skills/{s}/SKILL.md`" for s in skills
    ) if skills else ""

    content = f"""---
name: {skill_dir_name}
description: {description}
metadata:
  openclaw:
    requires:
      bins: ["python3"]
---

# {name}

{description}

## Capabilities

{caps_list}

## Agent Files

- Agent directory: `/home/alex/.openclaw/workspace/agents/{slug}/`
- Registry entry: `{slug}` in `/home/alex/.openclaw/workspace/agents/registry.json`
{f'''
## Referenced Skills

{skills_list}
''' if skills_list else ''}
## Model

Uses `{model}` for interactions.
"""

    skill_md_path.write_text(content)
    print(f"  Created SKILL.md: {skill_md_path}")


def _restart_gateway():
    """Restart the OpenClaw gateway so new skills/commands are registered."""
    import subprocess
    try:
        result = subprocess.run(
            ["systemctl", "--user", "restart", "openclaw-gateway.service"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            print("  Gateway restarted — new Telegram slash command will be live in ~5s")
        else:
            print(f"  WARNING: Gateway restart failed: {result.stderr.strip()}")
    except Exception as e:
        print(f"  WARNING: Could not restart gateway: {e}")


# ─── BUILD (Agent Builder) ─────────────────────────────────────────────

def build_agent(args):
    """Generate a prompt for the Agent Builder to create a new agent."""
    registry = load_registry()
    existing = list(registry.get("agents", {}).keys())

    prompt = f"""You are ARIA's Agent Builder. Design and create a new specialized agent.

## TASK
Create an agent for: {args.description}

## EXISTING AGENTS (do not duplicate)
{json.dumps(existing, indent=2)}

## REQUIREMENTS
1. Choose a unique slug (lowercase, no spaces, descriptive)
2. Write a focused personality (2-3 sentences, specialist mindset)
3. Select the right model:
   - "anthropic/claude-sonnet-4-5" — complex reasoning, research, strategy
   - "ollama/huihui_ai/glm-4.7-flash-abliterated" — coding, scripting, local tasks (free, mostly uncensored)
   - "ollama/huihui_ai/glm-4.7-flash-abliterated" — uncensored/NSFW content
4. List capabilities (what this agent can do)
5. List triggers (keywords that route tasks to this agent)
6. Reference existing skills if applicable

## OUTPUT FORMAT
Output a JSON object that can be passed to manage.py create:

```json
{{
  "slug": "myagent",
  "name": "My Agent",
  "command": "/myagent",
  "description": "One-line description",
  "personality": "2-3 sentence personality description",
  "model_primary": "model/id",
  "model_fallback": "model/id or null",
  "use_uncensored": false,
  "capabilities": ["cap1", "cap2"],
  "skills": ["aria-skillname"],
  "triggers": ["keyword1", "keyword2"],
  "spawn_method": "session",
  "timeout_seconds": 600
}}
```

## AFTER CREATION
After outputting the spec, run:
```
exec: {{ "command": "python3 /home/alex/.openclaw/workspace/agents/manage.py create <slug> --name '<name>' --model '<model>' --description '<desc>' --capabilities '<caps>' --skills '<skills>' --triggers '<triggers>'", "host": "gateway" }}
```

Then write a proper AGENT.md file with full personality and instructions to:
/home/alex/.openclaw/workspace/agents/<slug>/AGENT.md
"""

    # Write prompt to file
    SPAWN_DIR.mkdir(parents=True, exist_ok=True)
    build_id = short_id()
    prompt_file = SPAWN_DIR / f"agentbuilder_{build_id}.md"
    prompt_file.write_text(prompt)

    print(f"BUILD_READY:")
    print(f"  prompt_file: {prompt_file}")
    print(f"  model: anthropic/claude-sonnet-4-5")
    print()
    print(f"To build the agent:")
    print(f"  1. Read {prompt_file}")
    print(f"  2. Spawn the agentbuilder agent with this prompt")
    print(f"  3. Or use llm-task for a quick design")


# ─── STATUS ─────────────────────────────────────────────────────────────

def agent_status(args):
    """Show status of agent tasks."""
    if not TASKS_DIR.exists():
        print("No agent tasks found.")
        return

    tasks = []
    for f in sorted(TASKS_DIR.glob("agent_task_*.json")):
        with open(f) as fh:
            tasks.append(json.load(fh))

    active = [t for t in tasks if t.get("status") in ("SPAWNED", "RUNNING")]
    completed = [t for t in tasks if t.get("status") in ("DONE", "SUCCESS")]
    failed = [t for t in tasks if t.get("status") in ("FAILED", "ERROR")]

    print(f"AGENT STATUS:")
    print(f"  Active: {len(active)}")
    print(f"  Completed: {len(completed)}")
    print(f"  Failed: {len(failed)}")

    if active:
        print(f"\nACTIVE TASKS:")
        for t in active:
            print(f"  [{t['task_id']}] {t['agent']}: {t['task'][:60]}")
            print(f"    Model: {t.get('model', '?')} | Spawned: {t.get('spawned_at', '?')}")


# ─── RESULT ─────────────────────────────────────────────────────────────

def record_result(args):
    """Record a task result from an agent."""
    if not TASKS_DIR.exists():
        print("ERROR: No tasks directory.")
        sys.exit(1)

    # Find the task file
    task_file = TASKS_DIR / f"agent_task_{args.task_id}.json"
    if not task_file.exists():
        # Try partial match
        matches = list(TASKS_DIR.glob(f"agent_task_*{args.task_id}*.json"))
        if not matches:
            print(f"ERROR: Task '{args.task_id}' not found.")
            sys.exit(1)
        task_file = matches[0]

    with open(task_file) as f:
        task = json.load(f)

    task["status"] = args.status.upper()
    task["summary"] = " ".join(args.summary) if args.summary else None
    task["completed_at"] = now_iso()

    with open(task_file, "w") as f:
        json.dump(task, f, indent=2)

    print(f"RESULT_RECORDED: {task['task_id']}")
    print(f"  Agent: {task['agent']}")
    print(f"  Status: {task['status']}")


# ─── CAPABILITIES ───────────────────────────────────────────────────────

def list_capabilities(args):
    """List all capabilities across all agents."""
    registry = load_registry()
    cap_map = {}

    for slug, agent in registry.get("agents", {}).items():
        for cap in agent.get("capabilities", []):
            cap_map.setdefault(cap, []).append(slug)

    print(f"{'CAPABILITY':<30} {'AGENTS'}")
    print("-" * 60)
    for cap, agents in sorted(cap_map.items()):
        print(f"{cap:<30} {', '.join(agents)}")


# ─── COMMANDS ───────────────────────────────────────────────────────────

def list_commands(args):
    """List all slash commands."""
    registry = load_registry()

    print(f"{'COMMAND':<15} {'AGENT':<18} {'DESCRIPTION'}")
    print("-" * 80)
    for slug, agent in sorted(registry.get("agents", {}).items()):
        if not agent.get("active", True):
            continue
        cmd = agent.get("command", f"/{slug}")
        name = agent.get("name", slug)
        desc = agent.get("description", "")[:50]
        print(f"{cmd:<15} {name:<18} {desc}")


# ─── MAIN ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="ARIA Agent Manager")
    subparsers = parser.add_subparsers(dest="cmd", help="Command")

    # route
    p = subparsers.add_parser("route", help="Route a message to the right agent")
    p.add_argument("message", help="Message or task to route")

    # spawn
    p = subparsers.add_parser("spawn", help="Prepare spawn prompt for an agent")
    p.add_argument("slug", help="Agent slug")
    p.add_argument("--task", "-t", required=True, help="Task description")
    p.add_argument("--model", "-m", help="Override model")
    p.add_argument("--uncensored", action="store_true", help="Force uncensored model")
    p.add_argument("--mission-id", default=None, help="Parent mission ID for traceability")

    # list
    p = subparsers.add_parser("list", help="List all agents")
    p.add_argument("--json", action="store_true")

    # show
    p = subparsers.add_parser("show", help="Show agent details")
    p.add_argument("slug", help="Agent slug")

    # create
    p = subparsers.add_parser("create", help="Create a new agent")
    p.add_argument("slug", help="Agent slug (lowercase, no spaces)")
    p.add_argument("--name", required=True)
    p.add_argument("--model", required=True, help="Primary model")
    p.add_argument("--description", required=True)
    p.add_argument("--personality", help="Personality description")
    p.add_argument("--fallback", help="Fallback model")
    p.add_argument("--uncensored-model", help="Uncensored model")
    p.add_argument("--use-uncensored", action="store_true")
    p.add_argument("--capabilities", help="Comma-separated capabilities")
    p.add_argument("--skills", help="Comma-separated skill names")
    p.add_argument("--triggers", help="Comma-separated trigger words")
    p.add_argument("--command", help="Slash command (default: /slug)")
    p.add_argument("--spawn-method", choices=["session", "llm-task"], default="session")
    p.add_argument("--max-concurrent", type=int, default=1)
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--created-by", default="manual")

    # build
    p = subparsers.add_parser("build", help="Generate agent builder prompt")
    p.add_argument("--description", "-d", required=True, help="What the agent should do")

    # status
    subparsers.add_parser("status", help="Show running agent tasks")

    # result
    p = subparsers.add_parser("result", help="Record a task result")
    p.add_argument("task_id", help="Task ID")
    p.add_argument("--status", "-s", required=True, choices=["success", "error", "failed"])
    p.add_argument("--summary", nargs="+", help="Result summary")

    # capabilities
    subparsers.add_parser("capabilities", help="List all capabilities")

    # commands
    subparsers.add_parser("commands", help="List all slash commands")

    args = parser.parse_args()

    if not args.cmd:
        parser.print_help()
        sys.exit(1)

    cmd_map = {
        "route": route_message,
        "spawn": spawn_agent,
        "list": list_agents,
        "show": show_agent,
        "create": create_agent,
        "build": build_agent,
        "status": agent_status,
        "result": record_result,
        "capabilities": list_capabilities,
        "commands": list_commands,
    }

    cmd_map[args.cmd](args)


if __name__ == "__main__":
    main()
