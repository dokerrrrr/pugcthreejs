/** Run the pinned pugc-editor browser codec with Node.js features only. */
import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const [action, codecPath, input, output] = process.argv.slice(2);
if (!["encode", "decode"].includes(action) || !codecPath || !input || !output) {
  console.error("Usage: node tools/pugc_portable.mjs encode|decode <codec-module> <input> <output>");
  process.exit(1);
}

const { clientCodec } = await import(pathToFileURL(resolve(codecPath)));
if (action === "encode") {
  const gameData = JSON.parse(await readFile(input, "utf8"));
  if (gameData.dataVersion !== 3) throw new Error("Expected game-coordinate v3 JSON.");
  await writeFile(output, await clientCodec.encode(gameData, output, 3));
} else {
  const { json, fileInfo } = await clientCodec.decode(await readFile(input), input);
  if (fileInfo.version !== 3 || !fileInfo.markerValid) {
    throw new Error("Expected a valid PUGC v3 wrapper.");
  }
  await writeFile(output, JSON.stringify(json, null, 2) + "\n", "utf8");
}
