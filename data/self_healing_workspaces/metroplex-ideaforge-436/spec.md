```markdown
# Elder-Care Safety & Support Companion — Agent Specification

## Overview
The Elder-Care Safety & Support Companion assists adult children monitoring aging parents with mental health conditions, frailty, or cognitive decline. It helps document incidents of verbal or physical aggression, provides safety resources, and connects users to protective services when needed. For example, 32-year-old Sarah lives with her 68-year-old mother who has borderline personality disorder. After a heated argument, Sarah fears for her safety but feels guilty about seeking help. The agent helps her document incidents, assess risk levels, and connect with local support services without feeling like she's abandoning her mother.

## Agent shape
The agent consists of:
1. **agent.yaml**: Defines the agent's metadata and configuration.
   ```yaml
   name: Elder-Care Safety & Support Companion
   description: Assists adult children monitoring an aging parent’s risky behaviors and connecting to protective resources.
   model: claude-sonnet-4-6
   telegram_bot_token_env: ELDERCARE_BOT_TOKEN
   ```
2. **skills/incident_logging/SKILL.md**: Describes the incident logging skill, including negation-aware matching and word-boundary constraints.
   ```markdown
   ---
   name: Incident Logging
   description: Logs incidents of verbal or physical aggression from the elderly parent.
   trigger: "incident|argument|aggression|threat"
   ---
   ```
3. **tests/test_e2e_sarah_logs_incident.py**: Tests the agent's ability to log incidents and handle negations.
4. **tests/test_e2e_sarah_requests_help.py**: Tests the agent's ability to provide safety resources.
5. **README.md**: A four-paragraph story introducing Sarah, showing the agent in action, providing an invocation example, and noting deployment configurations.

## Constraints
- The agent runs entirely on the user's CCOS instance.
- No external services or API keys are hardcoded.
- Skills are bundled in the agent directory, not loaded from a global registry.
- No web frontend in T1.
- The agent is single-purpose, focused on elder-care safety.

## Safety constraints
- **Incident Logging**: Must reject negations like "I didn't have an argument" and word-boundary collisions like "argumentative". Must log all silent drops. The E2E test suite must include both positive and negative test cases for these scenarios.

## Success criteria
1. The agent correctly logs incidents of aggression and provides safety resources.
2. The E2E tests pass, including negation and word-boundary test cases.
3. The agent is invokable via `claude --agent eldercare` and `mission-cli`.

## Out of scope (T1)
- Multi-user partitioning
- Voice synthesis
- Cross-session memory
- Web frontend
- Calendar integration
- External HTTP services
- Insurance claim submission
```