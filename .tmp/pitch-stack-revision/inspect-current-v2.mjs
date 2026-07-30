import fs from "node:fs/promises";
import { FileBlob, PresentationFile } from "@oai/artifact-tool";

const deck = await PresentationFile.importPptx(
  await FileBlob.load("/Users/faizansheikh/ChhotuAI/ChhotuAI_Investor_Pitch.pptx"),
);
const snapshot = await deck.inspect({
  kind: "slide,textbox,shape,image,notes,layout",
  include: "id,slide,name,title,text,textPreview,textChars,textLines,bbox,bboxUnit",
  maxChars: 50000,
});
await fs.writeFile(
  "/Users/faizansheikh/ChhotuAI/.tmp/pitch-stack-revision/current-v2.ndjson",
  snapshot.ndjson,
);
