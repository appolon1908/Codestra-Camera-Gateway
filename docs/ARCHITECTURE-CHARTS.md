# Codestra-Camera-Gateway — Architecture Charts

> Repository: `appolon1908/Codestra-Camera-Gateway`  
> Baseline branch: `main`  
> Repository-local visual architecture. Keep these diagrams aligned with implementation, contracts and runtime boundaries.

## 1. System context
```mermaid
flowchart LR
 A["Cameras / operators"] --> B["Camera Gateway API"]
 B --> R["Codestra-Camera-Gateway<br/>Camera/RTSP ingestion gateway"]
 R --> S["camera/session config"]
 R --> D["FACE-ID / liveness / media consumers"]
```

## 2. Internal component architecture
```mermaid
flowchart TB
 I["Entrypoint / UI / API"] --> P["Identity, policy, validation"]
 P --> C["Core domain / orchestration"]
 C --> S["State / configuration / persistence"]
 C --> A["Adapters / integrations"]
 A --> X["Approved dependencies"]
 C --> O["Metrics, logs, traces, audit"]
```

## 3. Critical runtime flow
```mermaid
sequenceDiagram
 participant U as Caller
 participant B as Codestra-Camera-Gateway
 participant P as Policy
 participant C as Core
 participant S as State
 participant X as Dependency
 U->>B: Request / event / action
 B->>P: Authenticate + validate
 P-->>B: Decision
 B->>C: Discover, connect, ingest frames and publish bounded streams
 C->>S: Read / persist state
 C->>X: Bounded integration
 X-->>C: Result / readback
 C-->>U: Normalized response / view
```

## 4. Deployment and promotion
```mermaid
flowchart LR
 F["Feature branch"] --> T["Tests / validation"]
 T --> PR["Pull request + review"]
 PR --> CI["CI green"]
 CI --> ST["Staging / isolated verification"]
 ST --> EX["Exact-SHA certification"]
 EX --> G{"Production approval?"}
 G -- No --> ST
 G -- Yes --> P["Production promotion"]
 P --> H["Health/readiness + rollback check"]
```

## 5. Observability and recovery
```mermaid
flowchart LR
 R["Codestra-Camera-Gateway"] --> M["Metrics"]
 R --> L["Logs / audit"]
 R --> T["Traces / correlation"]
 M --> O["Observability stack"]
 L --> O
 T --> O
 O --> A["Dashboards / alerts"]
 R --> B["Backup / config snapshot"]
 B --> RR["Restore / rollback rehearsal"]
```

## Ownership notes
- **Role:** Camera/RTSP ingestion gateway
- **Primary boundary:** Camera Gateway API
- **State/config:** camera/session config
- **Dependencies/consumers:** FACE-ID / liveness / media consumers
- Cross-repository effects must use reviewed contracts; production effects remain separately gated.
