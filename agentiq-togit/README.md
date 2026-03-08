# AgentIQ – Epic 1 (Integration Hub) – Starter UI (Mock)

This is a buildable React + TypeScript + Tailwind UI implementation for **Epic 1 / Screen 1 v3**.

## Run
```bash
npm install
npm run dev
```

Open: http://localhost:5173/integration-hub

## Notes
- Connector data is loaded from `src/data/mockConnectors.json`
- State changes are in-memory via React Context (`ConnectorContext`)
- "View data" and "Start discovery" show a toast placeholder
- "Upload Files Instead" navigates to `/source-intake` placeholder
