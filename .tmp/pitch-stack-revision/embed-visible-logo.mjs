import fs from "node:fs/promises";
import { FileBlob, PresentationFile } from "@oai/artifact-tool";

process.on("uncaughtException", (error) => {
  console.error("LOGO_EMBED_ERROR:", error?.message ?? String(error));
  process.exit(1);
});

const input = "/Users/faizansheikh/ChhotuAI/ChhotuAI_Investor_Pitch.pptx";
const output = "/Users/faizansheikh/ChhotuAI/ChhotuAI_Investor_Pitch.pptx";
const deck = await PresentationFile.importPptx(await FileBlob.load(input));
const darkMarkBuffer = await fs.readFile(
  "/Users/faizansheikh/ChhotuAI/.tmp/pitch-stack-revision/logo-raster/chhotu-mark-dark-slide.png",
);
const lightMarkBuffer = await fs.readFile(
  "/Users/faizansheikh/ChhotuAI/.tmp/pitch-stack-revision/logo-raster/chhotu-mark-light-slide.png",
);
const darkMarkBytes = darkMarkBuffer.buffer.slice(
  darkMarkBuffer.byteOffset,
  darkMarkBuffer.byteOffset + darkMarkBuffer.byteLength,
);
const lightMarkBytes = lightMarkBuffer.buffer.slice(
  lightMarkBuffer.byteOffset,
  lightMarkBuffer.byteOffset + lightMarkBuffer.byteLength,
);

for (const [index, slide] of deck.slides.items.entries()) {
  const oldWordmarks = slide.shapes.items.filter(
    (shape) => shape.text?.toString().trim() === "Chhotu.ai",
  );
  for (const oldWordmark of oldWordmarks) oldWordmark.delete();

  const oldIcons = slide.images.items.filter((image) => {
    const frame = image.frame;
    return (
      image.alt === "Chhotu.ai logo" ||
      image.alt === "Chhotu.ai mark" ||
      (Math.abs(frame.left - 50) < 4 &&
        Math.abs(frame.top - 28) < 6 &&
        frame.width >= 150 &&
        frame.width <= 300 &&
        frame.height >= 45 &&
        frame.height <= 80) ||
      Math.abs(frame.left - 50) < 4 &&
      Math.abs(frame.top - 42) < 4 &&
      frame.width < 60 &&
      frame.height < 60
    );
  });
  for (const oldIcon of oldIcons) oldIcon.delete();

  const isDarkSlide = index < 3;
  slide.images.add({
    blob: isDarkSlide ? darkMarkBytes : lightMarkBytes,
    contentType: "image/png",
    alt: "Chhotu.ai mark",
    fit: "contain",
    position: { left: 50, top: 36, width: 28, height: 28 },
    lockAspectRatio: true,
  });

  const wordmark = slide.shapes.add({
    geometry: "textbox",
    name: "Chhotu.ai wordmark",
    position: { left: 84, top: 33, width: 112, height: 34 },
    fill: "none",
    line: { style: "solid", fill: "none", width: 0 },
  });
  wordmark.text.set([[
    {
      run: "Chhotu",
      textStyle: {
        bold: true,
        color: isDarkSlide ? "#F7F8F8" : "#111814",
      },
    },
    {
      run: ".ai",
      textStyle: {
        bold: true,
        color: isDarkSlide ? "#59F2BC" : "#00865F",
      },
    },
  ]]);
  wordmark.text.style = {
    fontSize: 20,
    typeface: "Arial",
    verticalAlignment: "middle",
  };
}

const snapshot = await deck.inspect({
  kind: "slide,textbox,image",
  include: "id,slide,name,text,textPreview,bbox,bboxUnit,alt",
  maxChars: 50000,
});
await fs.writeFile(
  "/Users/faizansheikh/ChhotuAI/.tmp/pitch-stack-revision/logo-fixed-inspect.ndjson",
  snapshot.ndjson,
);
const pptx = await PresentationFile.exportPptx(deck);
await pptx.save(output);
