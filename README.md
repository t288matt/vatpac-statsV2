# VATPAC Stats v3

Live pilot and controller statistics for the VATPAC region, built from the VATSIM data feed. Small, dockerised, and always on.

- Spec: [SPEC.md](SPEC.md)
- Decisions: [docs/DECISIONS.md](docs/DECISIONS.md)

## Run it

```
cp .env.example .env     # then edit the passwords
docker compose up -d --build
```

Grafana: http://localhost:3300 (login from `.env`).

Status: under construction. See SPEC.md section 12 for the acceptance checks.
