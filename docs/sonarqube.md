# SonarQube

## 1) Purpose
Use SonarQube for static analysis, code-smell/security checks, duplication detection, and quality-gate enforcement.

## 2) Local Setup
1. Start SonarQube server and database (for local learning, Docker Compose is fine).
2. Create a user token in SonarQube UI.
3. Export token locally:
   - `export SONAR_TOKEN="<your-token>"`

This repo is already configured via `sonar-project.properties`.

## 3) Local Scan (Project Root)
```bash
./venv/bin/pytest --cov=. --cov-report=xml:coverage.xml -q
sonar-scanner "-Dsonar.host.url=http://localhost:9000" "-Dsonar.token=$SONAR_TOKEN"
```

Notes:
- Always generate `coverage.xml` before scanning.
- Run from repository root so `sonar-project.properties` is picked up.
- In `zsh`, quote `-D` values that contain glob patterns.

## 4) CI Integration
CI workflow `.github/workflows/ci.yml` includes job `sonar-quality-gate` that:
1. installs dependencies,
2. runs full tests with coverage xml,
3. runs Sonar scanner in Docker (`sonarsource/sonar-scanner-cli`).

Required repository settings:
- secret: `SONAR_TOKEN`
- variable: `SONAR_HOST_URL`

If either value is missing, the workflow skips the Sonar step and prints a clear message.

## 5) Troubleshooting
- `command not found: sonar-scanner`
  - install scanner locally (`brew install sonar-scanner` on macOS) or use CI job.
- `Communicating with SonarQube Cloud` + `403`
  - scanner is targeting SonarCloud; pass your self-hosted URL with `-Dsonar.host.url=...`.
- coverage gate fails near threshold
  - inspect New Code coverage in Sonar and add tests for low-coverage touched files first.
