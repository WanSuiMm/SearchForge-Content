import { copyFile } from "node:fs/promises";

await copyFile(new URL("../dist/app.js", import.meta.url), new URL("../app.js", import.meta.url));
