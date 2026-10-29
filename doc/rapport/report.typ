// =============================================================================
// Cloud Systems - Project 2 Report: Real-Time IoT Device Monitoring & Cloud Dashboarding
// Group 10 — SDATAWAY Smart Capsule Coffee Machine Fleet
// =============================================================================

#set document(
  title: "Cloud Systems - Project 2 Report (Group 10)",
  author: ("Nathan Coudray", "Thoma Chalon", "Basile Gasser"),
  date: auto,
)

#set page(
  paper: "a4",
  margin: (top: 1.4cm, bottom: 1.4cm, left: 1.6cm, right: 1.6cm),
  numbering: "1",
)

#set text(
  font: ("Liberation Sans", "DejaVu Sans", "Arial", "Helvetica"),
  size: 8.7pt,
  lang: "en",
)

#set par(
  justify: true,
  leading: 0.52em,
  spacing: 0.6em,
)

#show heading.where(level: 1): it => block(
  above: 0.8em,
  below: 0.5em,
)[
  #text(weight: "bold", size: 11pt, fill: rgb("#0f172a"))[#it.body]
  #v(1pt)
  #line(length: 100%, stroke: 0.6pt + rgb("#cbd5e1"))
]

#show heading.where(level: 2): it => block(
  above: 0.55em,
  below: 0.35em,
)[
  #text(weight: "bold", size: 9.2pt, fill: rgb("#1e293b"))[#it.body]
]

// -----------------------------------------------------------------------------
// HEADER METADATA (Strictly matching template)
// -----------------------------------------------------------------------------
#grid(
  columns: (auto, 1fr),
  row-gutter: 4pt,
  column-gutter: 8pt,
  [*Project :*], [Real-Time IoT Device Monitoring & Cloud Dashboarding Platform],
  [*Group :*], [Group 10],
  [*Students :*], [Nathan Coudray, Thoma Chalon, Basile Gasser],
  [*Git URL (if applicable) :*], [#link("https://github.com/chatLong2000/cloud_project_2_grp10")[https://github.com/chatLong2000/cloud_project_2_grp10]],
  [*Link to a video :*], [
    // Video Link Placeholder - replace with your final link
    #link("https://youtu.be/placeholder")[https://youtu.be/demo-video-link-placeholder]
    #text(size: 7.5pt, fill: rgb("#b91c1c"))[(Placeholder — insert your video link here)]
  ],
)

#v(1pt)

// =============================================================================
// SECTION 1: ARCHITECTURE
// =============================================================================
= Architecture

== 1. A figure detailing the architecture

#figure(
  image("architecture.svg", width: 100%),
  caption: [End-to-End IoT Pipeline: Data Ingestion, Multi-Measure Time-Series Storage, Scenario Analysis, and Monitoring],
)

== 2. For each component: two lines of text (maximum) explaining the component

- *50 Coffee Machines (Simulators):* 50 concurrent Python processes simulating ESP32 microcontrollers, streaming 8-field CSV telemetry frames every 2 seconds over virtual serial UART ports (`socat` PTY tunnels).
- *Edge Gateway (Docker):* Local multi-threaded service reading serial streams, validating CSV metrics, buffering records in SQLite (WAL mode, store-and-forward), and publishing MQTT batches ($<= 60$ s).
- *AWS IoT Core (MQTT Broker):* Managed serverless broker securing edge communication via mutual TLS (X.509 certificates) and routing incoming batches on `coffee-GRP10/+/batch` via an SQL Topic Rule.
- *AWS Lambda Ingestor:* Stateless Python FaaS function triggered per batch, packaging measures into Multi-Measure records and batch-writing up to 100 items per API call into Amazon Timestream.
- *Amazon Timestream (Database):* Serverless time-series database (`SdatawayIoTDB`) holding telemetry and alerts, automatically tiering data from Memory Store (24h) to Magnetic Store (90 days / 3 months).
- *Scenario Analyzer Worker:* Continuous Python analytics engine on EC2 running sliding-window SQL queries to detect anomalies (`thermal_runaway`, leaks, OOM) and silent machines ($>$ 5 min).
- *Grafana OSS 13.0 Dashboard:* Web visualization server hosted on EC2 with the Infinity plugin, presenting 5-minute real-time fleet health, active statuses, and per-machine metric drill-downs.
- *Amazon SNS (Alerting):* Managed Pub/Sub notification service (`GRP10-SdatawayAlertsTopic`) instantly fanning out critical alarm notifications to operating staff via email and SMS.
- *Amazon CloudWatch Logs:* Centralized logging service capturing system, broker, Lambda, and worker execution logs with an enforced 1-year (365 days) audit retention policy.

// Explicit page break: Guarantees strict 2-page document structure
#pagebreak()

// Running Header on Page 2
#grid(
  columns: (1fr, 1fr),
  align: (left, right),
  text(size: 7.5pt, fill: rgb("#64748b"))[Cloud Systems — Project 2 Report (Group 10)],
  text(size: 7.5pt, fill: rgb("#64748b"))[Page 2 of 2],
)
#v(-3pt)
#line(length: 100%, stroke: 0.4pt + rgb("#cbd5e1"))
#v(2pt)

// =============================================================================
// SECTION 2: DEPLOYMENT
// =============================================================================
= Deployment

For each component, the specific Cloud service and deployment model are detailed below:

- *Edge Gateway & Simulators (Local Docker Environment):* Deployed locally in Docker containers (`docker compose`). Running the Edge outside AWS fulfills SDATAWAY's physical Raspberry Pi constraint, validates offline store-and-forward persistence across network outages, and incurs \$0 in cloud runtime costs.
- *AWS IoT Core (PaaS):* Managed serverless MQTT broker in AWS `us-east-1`, secured via client X.509 certificates and an IoT Policy restricting publishing to `coffee-GRP10/+/batch`. Automatically routes batches to Lambda via an SQL Topic Rule.
- *AWS Lambda Ingestor (FaaS):* Stateless event-driven serverless function (`Python 3.12`, x86_64, 128 MB RAM, 15 s timeout). Scales dynamically with zero idle cost; execution IAM role is strictly scoped to `timestream:WriteRecords` and CloudWatch logging.
- *Amazon Timestream (PaaS — Time-Series Database):* Serverless time-series database (`SdatawayIoTDB`) with `telemetry` and `alerts` tables using multi-measure records. Uses automated dual-tier storage: 24-hour Memory Store for real-time dashboards and 90-day Magnetic Store (\$0.03/GB/mo) for 3-month retention.
- *Scenario Analyzer & Grafana OSS (IaaS — Amazon EC2 VM):* Deployed on a single Amazon EC2 `t3.small` instance (Amazon Linux 2023) in the default VPC, hosting a containerized Docker Compose stack (`analyser` worker + `grafana` OSS 13.0):
  - _Access & Security:_ Managed via AWS Systems Manager (SSM Session Manager) with SSH (port 22) disabled. IAM Instance Profile grants least-privilege Timestream query, SNS publish, and CloudWatch logging rights.
  - _Networking:_ Grafana is served on port 3000 with ingress restricted to authorized operator CIDRs via Security Groups. Grafana admin credentials are securely managed through SSM Parameter Store.
- *Amazon SNS & Amazon CloudWatch Logs (PaaS):* SNS topic `GRP10-SdatawayAlertsTopic` dispatches email alerts for critical scenarios; CloudWatch captures IoT Core, Lambda, and Analyzer logs with an enforced 365-day (1-year) audit retention schedule.

// =============================================================================
// SECTION 3: TASKS
// =============================================================================
= Tasks

List of tasks realised within the project, their concrete outcomes, and student involvement:

#v(2pt)

#align(center)[
#table(
  columns: (2.1fr, 4.5fr, 1.9fr),
  stroke: (x, y) => if y == 0 { (bottom: 1.2pt + rgb("#1e293b"), top: 0.6pt + rgb("#cbd5e1")) } else { 0.4pt + rgb("#e2e8f0") },
  fill: (col, row) => if row == 0 { rgb("#f1f5f9") } else if calc.even(row) { rgb("#f8fafc") } else { none },
  inset: (x: 4.5pt, y: 3.2pt),
  align: (col, row) => if row == 0 { center + horizon } else if col == 2 { center + horizon } else { left + horizon },
  table.header(
    [*Tasks*], [*Outcome*], [*Students involved*]
  ),
  [1. Initial Architecture Definition],
  [Designed the end-to-end cloud pipeline (`ESP32 → UART → Edge → MQTT → IoT Core → Lambda → Timestream → VM/Grafana/Analyzer → SNS/CloudWatch`) meeting SDATAWAY constraints and SLAs (< 5 min latency).],
  [#text(fill: luma(110))[To be assigned]],

  [2. Database Technology Selection & Trade-Off],
  [Researched and benchmarked storage options (Amazon Timestream vs. DynamoDB, InfluxDB), establishing the multi-measure schema and 3-month cost/sizing model (~$20–$35/month).],
  [Basile Gasser],

  [3. Coffee Machine Fleet Simulation],
  [Containerized 50 concurrent ESP32 simulators generating 8-field CSV telemetry frames (0.5 Hz) over `socat` virtual serial PTY channels.],
  [#text(fill: luma(110))[To be assigned]],

  [4. Edge Gateway (Serial, Buffer & MQTT)],
  [Developed multi-threaded Docker gateway reading serial streams, validating CSV metrics, buffering in SQLite (WAL mode, store-and-forward), and publishing MQTT batches ($<= 60$ s).],
  [#text(fill: luma(110))[To be assigned]],

  [5. AWS IoT Core Ingestion & CloudWatch],
  [Configured managed IoT Core broker, X.509 mutual TLS authentication, topic routing rules (`coffee-GRP10/+/batch`), and broker connection logging in CloudWatch.],
  [#text(fill: luma(110))[To be assigned]],

  [6. AWS Lambda Ingestor Component],
  [Built and deployed stateless Python FaaS `ingestor` unpacking incoming MQTT batches and inserting records via the Timestream `WriteRecords` batch API (up to 100 records/call).],
  [Basile Gasser],

  [7. Amazon Timestream Database Deployment],
  [Automated provisioning of database `SdatawayIoTDB` with `telemetry` and `alerts` tables, configuring lifecycle retention (24h Memory Store, 90d Magnetic Store).],
  [Basile Gasser],

  [8. Monitoring VM (Grafana OSS & Scenario Analyzer)],
  [Deployed EC2 VM (`t3.small`) hosting Docker Compose stack with Scenario Analyzer (sliding-window detection of thermal runaway, leaks, OOM, silences) and Grafana OSS 13.0 dashboards.],
  [#text(fill: luma(110))[To be assigned]],

  [9. Amazon SNS Alerting System],
  [Provisioned topic `GRP10-SdatawayAlertsTopic` with email/SMS subscriptions and integrated the alert dispatching mechanism in the Scenario Analyzer for critical fault notifications.],
  [Basile Gasser],

  [10. CloudWatch Logging for DB & Analyzer],
  [Implemented centralized CloudWatch logging for Timestream ingestion tracking and Scenario Analyzer application logs, enforcing the mandatory 365-day (1-year) retention policy.],
  [Basile Gasser],

  [11. Demonstration Video],
  [Recorded and edited the platform video demonstration showcasing telemetry generation, real-time edge batching, cloud storage, anomaly detection, and Grafana dashboarding.],
  [#text(fill: luma(110))[To be assigned]],

  [12. Project Report (Typst Documentation)],
  [Authored the 2-page project documentation in Typst according to the template, detailing architecture, component roles, cloud deployment models, and task breakdown.],
  [Basile Gasser],
)
]
