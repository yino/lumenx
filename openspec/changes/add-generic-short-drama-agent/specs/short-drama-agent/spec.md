## ADDED Requirements

### Requirement: Agent runtime is isolated from provider execution
The system SHALL place the generic short-drama Agent runtime under `src/agents/` and SHALL invoke model execution only through existing platform services, the Cloud AI Gateway, and Provider Adapters.

#### Scenario: Agent creates a video task
- **WHEN** the Agent reaches the submission node with an approved `ShotPackage`
- **THEN** it calls the internal task service or Gateway with a structured request and never constructs a Provider Adapter or external HTTP request directly

#### Scenario: Agent attempts an unsupported direct provider call
- **WHEN** an Agent node requests a provider operation that is not exposed by an internal tool contract
- **THEN** the run enters a failed or blocked state with a structured policy error and no provider request is submitted

### Requirement: Short-drama workflow uses a typed resumable state
The system SHALL represent each Agent Run with a versioned, JSON-serializable state containing the project, target profile, shot packages, asset bindings, validation report, approval state, and submitted task IDs.

#### Scenario: Resume after worker interruption
- **WHEN** a Celery worker stops after a completed node and the Agent Run is restarted
- **THEN** the runtime resumes from the latest valid PostgreSQL checkpoint without repeating completed side effects

#### Scenario: Duplicate submission retry
- **WHEN** the submission node is retried with the same run and shot idempotency key
- **THEN** the system returns or reuses the existing video task instead of creating a duplicate billable task

### Requirement: Workflow stages are explicit and observable
The system SHALL expose the short-drama stages `intake`, `storyboard_plan`, `continuity_check`, `asset_binding`, `prompt_compile`, `quality_gate`, `human_approval`, `submit_task`, and `monitor_task` as identifiable state transitions.

#### Scenario: Stage progress is queried
- **WHEN** a client requests the status of an Agent Run
- **THEN** the response includes the current stage, stage status, last transition time, and any blocking findings

#### Scenario: A node produces invalid output
- **WHEN** a node output fails its Pydantic contract
- **THEN** the transition is rejected, the run records the validation error, and the next stage is not entered

### Requirement: Human approval gates billable execution
The system SHALL require an explicit approval decision for any run whose quality report contains blocking findings or warnings configured as approval-required before submitting a video task.

#### Scenario: Blocking finding exists
- **WHEN** the quality gate reports a blocking missing reference or invalid timeline
- **THEN** the run becomes `blocked` and the submit node cannot execute

#### Scenario: User approves warnings
- **WHEN** the quality gate reports only approval-required warnings and the authorized user approves the run
- **THEN** the run records the approver and proceeds to submission using the same validated state

### Requirement: Existing Seedance Skill remains a compatibility entry point
The system SHALL accept `seedance-short-drama` as an alias for the generic short-drama Agent and SHALL resolve its default target to a configured Seedance Profile without duplicating the workflow implementation.

#### Scenario: Legacy Skill invocation
- **WHEN** a request identifies `seedance-short-drama` and does not specify another profile
- **THEN** the system starts the generic Agent with the Seedance Profile and records the alias used

#### Scenario: Explicit alternate profile
- **WHEN** a request uses the same generic workflow with an explicitly allowed Grok or other Profile
- **THEN** the system uses that Profile while retaining the same typed workflow and quality gates
