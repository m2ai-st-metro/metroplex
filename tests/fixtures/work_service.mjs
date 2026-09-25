// Starts a real Teletraan work service for Metroplex integration tests.
// Usage: node work_service.mjs TELETRAAN_ROOT STATE_DIR APPROVER_ID
// Prints one JSON line {ready, socket, tokens} and serves until killed.
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const [root, stateDir, approverId] = process.argv.slice(2);
const { WorkStore } = await import(pathToFileURL(join(root, "src/domain/work.mjs")).href);
const { listenWorkService } = await import(pathToFileURL(join(root, "src/api/work-service.mjs")).href);
const tokens = { operator: "operator-token-000000000000", cos: "cos-token-00000000000000000", runtime: "runtime-token-0000000000000", owner: "owner-token-000000000000000", worker: "worker-token-00000000000000" };
const store = new WorkStore(join(stateDir, "work.sqlite"), { approverIds: [approverId] });
const socket = join(stateDir, "work.sock");
const server = await listenWorkService({ store, socketPath: socket, credentials: tokens });
console.log(JSON.stringify({ ready: true, socket, tokens }));
for (const signal of ["SIGTERM", "SIGINT"]) process.on(signal, () => server.close(() => { store.close(); process.exit(0); }));
