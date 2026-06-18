import fs from "fs";
import path from "path";

// Build the webpack entry map from the scenario category folders under
// src/, so each scenario keeps its category in the output path. A file
// src/<category>/<name>-test.js becomes the entry <category>/<name>-test,
// and with filename "[name].js" lands in dist/<category>/<name>-test.js.
// Categories: network (synthetic, protocol-level) and browser (driven
// through a real Chromium).
function scenarioEntries() {
    const srcDir = path.resolve(import.meta.dirname, "src");
    const entries = {};
    for (const category of fs.readdirSync(srcDir)) {
        const categoryDir = path.join(srcDir, category);
        if (!fs.statSync(categoryDir).isDirectory()) continue;
        for (const file of fs.readdirSync(categoryDir)) {
            if (!file.endsWith("-test.js")) continue;
            const name = path.basename(file, ".js");
            entries[`${category}/${name}`] = `./src/${category}/${file}`;
        }
    }
    return entries;
}

export default {
    mode: "development",
    entry: scenarioEntries(),
    output: {
        libraryTarget: "commonjs",
        path: path.resolve(import.meta.dirname, "dist"),
        filename: "[name].js"
    },
    externals: /^(k6|https?\:\/\/)(\/.*)?/,
}
