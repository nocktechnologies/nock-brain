# Try Brain through conversation

This small, assisted pilot tests whether an agent can handle memory naturally:
you tell it what to remember, ask about it later, and correct or forget it when
needed. Explorer is available when you want to inspect the store. Start with
a few invented facts; use your own selected notes once the flow makes sense.
This is a pilot, not a stable consumer release.

## Start a fresh Brain

You need Git, Python 3.10+ and a working Claude Code installation on macOS or
Linux. Use a Claude setup without fleet integrations for an isolation trial;
inspect `/hooks` before entering pilot data. Existing user, project, managed
or plugin hooks can still run. A separate Brain does not isolate the whole
Claude session. Text the agent reads is processed through your Claude setup;
local storage does not mean the conversation runs offline.

Clone the repository, or use an existing checkout. Do not run `install.sh`.

```sh
git clone https://github.com/nocktechnologies/nock-brain.git
cd nock-brain
```

Choose a new absolute store path outside the checkout. Its parent must exist
and the destination itself must not. From the checkout root:

```sh
STORE="$HOME/customer-brain-pilot"
python3 bin/consumer-brain.py init --store "$STORE"
claude --append-system-prompt-file "$PWD/docs/customer-agent.md" \
  "Use this checkout and the customer Brain at $STORE. Check its status and help me manage memories through conversation."
```

This loads the [agent playbook](customer-agent.md) using Claude's native
[prompt-file flag](https://code.claude.com/docs/en/cli-reference#system-prompt-flags).
Start a fresh conversation so the guide is loaded. Reuse the same launch
command for later sessions, without rerunning `init`. Nothing in this setup
automatically captures conversations. Normal Claude tool permission prompts
still apply; the playbook does not enforce approval independently of the agent.

## Try a short conversation

Use these invented examples, one at a time:

1. “Remember this exact decision: Ship the Willow workshop report on Friday.”
   The agent should save that decision and tell you the result. Your explicit
   request is approval; it should ask again only if the actual extracted
   content or scope differs.
2. “What do you have stored about the Willow workshop report?” Check the
   wording. Ask where the note came from: it should be described as a curated
   note from your request, not an invented original transcript.
3. Exit and start a **fresh** conversation with the same launch command.
   Ask “What did we decide about the Willow workshop report?” Then ask
   “When is that workshop write-up due?” Record whether the answers were useful
   and based on stored facts. This checks persistence beyond conversation history.
4. “Change that decision: ship the Willow workshop report on Monday.” The
   agent should correct the selected record. Ask again and check Monday is
   current; Friday remains available only as superseded history.
5. “Forget the Monday workshop report decision.” The agent should explain
   that this also clears all saved proposals and does not erase original
   sources, other records or backups. Approve that scope, then check the
   record is gone. If you opened Explorer, refresh it too.

Optionally ask the agent to open Explorer to check the accepted facts, source
evidence and verification labels. Its browser view is read-only. It is not
required for any of these conversations.

## Try capture only if useful

After the manual flow works, follow the [session hook setup](customer-setup.md#optional-capture-and-recall-in-claude-code)
for one explicitly selected transcript directory. Launch a fresh session with
both the generated settings and the playbook (from the checkout root, with
`STORE` set to the same absolute destination):

```sh
claude --settings "$STORE/claude-settings.json" \
  --append-system-prompt-file "$PWD/docs/customer-agent.md" \
  "Use this checkout and the customer Brain at $STORE. Check its status."
```

Inspect `/hooks` again. Say “For this example, [DECISION] Use blue covers for
the Willow workshop. Leave it pending until I review it.” After capture, ask
“Show me the pending memories.” Check it is absent from accepted memory.
Approve just the desired proposal, or reject it and check the queue. If a
batch mixes wanted and unwanted facts, the agent must repropose the selected
facts instead of applying the entire batch. Then try direct and paraphrased
questions in a new session. Record automatic hook recall separately from an
agent manually looking up a record: the hook uses BM25, depends on shared
terms and has no semantic tier. A successful manual lookup does not prove
automatic recall worked.

## Feedback and the next decision

Try this with a few people before adding more interface. Each person should
keep their own fresh store; never distribute a populated fleet store. Record:

- OS, Python and Claude versions, key algorithm, and time to first saved fact.
- Useful memories saved, useful facts missed, and irrelevant or inaccurate
  candidates rejected. Note attribution mistakes and repeated confirmations.
- Useful answers out of attempted memory questions, including fresh sessions;
  distinguish manual lookup from automatic recall when hooks are enabled.
- Setup friction, correction/forgetting surprises, and any moment conversation
  was insufficient and a separate review screen would have helped.

Share counts and invented or manually sanitized examples. Do not send raw
transcripts, signing keys or copied stores. Before calling the pilot successful,
also check that reimporting a forgotten fact skips its exact ID and that a
second fresh store starts empty with a different identity; the
[command guide](customer-setup.md) gives the operations.

Build an inbox only if users repeatedly lose track of proposals or need to
review more than conversation handles comfortably. A stable release still
needs repeatable setup outside the team, useful extraction and recall on real
work, understandable removal controls, and a supported distribution/update
path. Automated synthetic tests and the 36-query fixture gate do not measure
these customer outcomes; no customer pass rate is claimed here.
