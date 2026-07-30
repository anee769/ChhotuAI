import fs from "node:fs/promises";
import { FileBlob, PresentationFile } from "@oai/artifact-tool";

const input = "/Users/faizansheikh/ChhotuAI/ChhotuAI_Investor_Pitch.pptx";
const deck = await PresentationFile.importPptx(await FileBlob.load(input));
const snapshot = await deck.inspect({
  kind: "slide,textbox,shape,image,notes",
  include: "id,slide,name,title,text,textPreview,textChars,textLines,bbox,bboxUnit",
  maxChars: 50000,
});
await fs.writeFile(
  "/Users/faizansheikh/ChhotuAI/.tmp/pitch-stack-revision/current-inspect.ndjson",
  snapshot.ndjson,
);

const ids = [
  "sh/k3yl0zql", "sh/7qp4be9c",
  "sh/9072xkry", "sh/ozy1ofad", "sh/q5wjelsz", "sh/ove9o7yd", "sh/s72xofmh",
  "sh/cza94vmx", "sh/d0jax03i", "sh/3ah8rqlg", "sh/obq90bml", "sh/pcjqtg36",
  "sh/n6ls3alk", "sh/n2l4fq98", "sh/943mhgre", "sh/83ulovat",
  "sh/x8nml0ra", "sh/w7ulsvqp", "sh/ja54na9g", "sh/i9w3u58v", "sh/e10f2twf",
  "sh/1cj2d8b6", "sh/0ba143al",
  "sh/dgbulwnm", "sh/cf2tcr61",
];
const details = {};
for (const id of ids) {
  const shape = deck.resolve(id);
  details[id] = {
    frame: shape.frame,
    text: shape.text?.toString?.() ?? "",
    textStyle: shape.text?.style,
  };
}
await fs.writeFile(
  "/Users/faizansheikh/ChhotuAI/.tmp/pitch-stack-revision/target-details.json",
  JSON.stringify(details, null, 2),
);
