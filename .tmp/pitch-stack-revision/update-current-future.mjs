import fs from "node:fs/promises";
import { FileBlob, PresentationFile } from "@oai/artifact-tool";

const input = "/Users/faizansheikh/ChhotuAI/ChhotuAI_Investor_Pitch.pptx";
const output = "/Users/faizansheikh/ChhotuAI/ChhotuAI_Investor_Pitch.pptx";
const deck = await PresentationFile.importPptx(await FileBlob.load(input));

const setText = (id, value) => {
  deck.resolve(id).text = value;
};

// Slide 4 — current, shipped capabilities.
setText("sh/rm1k7yt4", "CURRENT PRODUCT / AVAILABLE NOW");
setText("sh/ql8jytsj", "What Chhotu.ai does today.");
setText(
  "sh/doj29oba",
  "Live workflows for hardware retailers, wholesalers and distributors.",
);
setText("sh/3ihk3et8", "AVAILABLE NOW / VOICE");
setText("sh/i94r6xgz", "AVAILABLE NOW / INVENTORY");
setText("sh/a5kr2xg3", "AVAILABLE NOW / CRM + BILLING");
setText("sh/p0batw72", "AVAILABLE NOW / DOCUMENT AI");
setText(
  "sh/gv29g7q5",
  "CURRENT: VOICE  •  INVENTORY  •  CRM  •  DOCUMENT AI",
);

// Slide 5 — roadmap, not a current-production claim.
setText("sh/z2tcnm5s", "FUTURE ROADMAP / NOT CURRENT");
setText("sh/yhkbe1o7", "What Chhotu.ai becomes next.");
setText(
  "sh/l4bupwny",
  "Roadmap expectations—not capabilities claimed in today’s production product.",
);
setText("sh/n6dcr65o", "IN DEVELOPMENT / ORDERS");
setText("sh/ahkvi1cb", "PLANNED / DELIVERY");
setText("sh/i54fmlc7", "PLANNED / SUPPLIER RESTOCK");
setText("sh/hgrmpwj2", "LONGER TERM / WAREHOUSE SCALE");
setText("sh/pkr6tgjy", "FUTURE CONCEPT: “Hey Chhotu…”");
setText("sh/4ji5kbid", "OPT-IN WAKE WORD  •  ON-DEVICE TRIGGER");

deck.resolve("sl/jyx0ra1s").speakerNotes.textFrame.setText(
  "Both: Everything on this slide is available in the current product: multilingual voice-led sales and purchases; inventory, stock and margin questions; customer credit and payment records; bills and reminders; and reviewed supplier-invoice digitisation. The grounded backend remains the system of record—Samvaad can request an action, but only validated tools write stock or money movements.\n\n[Sources]\n- /Users/faizansheikh/ChhotuAI/README.md\n- /Users/faizansheikh/ChhotuAI/frontend/index.html\n- /Users/faizansheikh/ChhotuAI/backend/main.py\n[/Sources]",
);
deck.resolve("sl/i107q5of").speakerNotes.textFrame.setText(
  "Speaker 2: This slide is explicitly the roadmap. Order lifecycle and grounded Sarvam order tools are in development on the dev branch and are not a current production claim. Provider-managed deliveries, supplier restocking calls, invoice reconciliation and warehouse-scale fulfilment analytics are planned phases. A 'Hey Chhotu' wake word is a future concept: it should be opt-in and privacy-preserving, with an on-device trigger that starts a Samvaad session only after activation.\n\n[Sources]\n- /Users/faizansheikh/ChhotuAI/docs/order-fulfilment-roadmap.md\n- /Users/faizansheikh/ChhotuAI/README.md\n[/Sources]",
);

// Verify that the logo image and joined Chhotu.ai wordmark remain present at
// the top-left on every slide.
for (const [slideId, wordmarkId] of [
  ["sl/y90nupkv", "sh/k3yl0zql"],
  ["sl/hwbqtkby", "sh/9072xkry"],
  ["sl/ofy9wn61", "sh/cza94vmx"],
  ["sl/jyx0ra1s", "sh/1cj2d8b6"],
  ["sl/i107q5of", "sh/dgbulwnm"],
]) {
  const slide = deck.resolve(slideId);
  const wordmark = deck.resolve(wordmarkId);
  if (wordmark.text.toString() !== "Chhotu.ai" ||
      wordmark.frame.left > 120 || wordmark.frame.top > 60 ||
      slide.images.items.length < 1) {
    throw new Error(`Missing top-left Chhotu.ai logo on ${slideId}`);
  }
}

const snapshot = await deck.inspect({
  kind: "slide,textbox,image,notes",
  include: "id,slide,text,textPreview,textChars,textLines,bbox,bboxUnit",
  maxChars: 50000,
});
await fs.writeFile(
  "/Users/faizansheikh/ChhotuAI/.tmp/pitch-stack-revision/current-future-inspect.ndjson",
  snapshot.ndjson,
);
const pptx = await PresentationFile.exportPptx(deck);
await pptx.save(output);
