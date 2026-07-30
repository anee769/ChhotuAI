import fs from "node:fs/promises";
import { FileBlob, PresentationFile } from "@oai/artifact-tool";

const input = "/Users/faizansheikh/ChhotuAI/ChhotuAI_Investor_Pitch.pptx";
const output = "/Users/faizansheikh/ChhotuAI/ChhotuAI_Investor_Pitch.pptx";
const deck = await PresentationFile.importPptx(await FileBlob.load(input));

function setText(id, value) {
  deck.resolve(id).text = value;
}

function joinBrand(wordId, suffixId, foreground) {
  const word = deck.resolve(wordId);
  word.frame = { left: 101, top: 46, width: 142, height: 30 };
  word.text.set([[
    { run: "Chhotu", textStyle: { bold: true, color: foreground } },
    { run: ".ai", textStyle: { bold: true, color: "#59F2BC" } },
  ]]);
  deck.resolve(suffixId).delete();
}

joinBrand("sh/k3yl0zql", "sh/7qp4be9c", "#F7F8F8");
joinBrand("sh/9072xkry", "sh/ozy1ofad", "#F7F8F8");
joinBrand("sh/cza94vmx", "sh/d0jax03i", "#F7F8F8");
joinBrand("sh/1cj2d8b6", "sh/0ba143al", "#111814");
joinBrand("sh/dgbulwnm", "sh/cf2tcr61", "#111814");

// Correct the sector comparison: 34.7% is the ASUSE 2023–24 internet-use
// figure for trade establishments; 26.7% was the all-sector figure.
setText("sh/q5wjelsz", "34.7%");
setText("sh/ove9o7yd", "trade establishments using internet");
setText("sh/nu58f2hs", "delayed-payment claims filed by MSEs");
setText(
  "sh/s72xofmh",
  "Sources: MoSPI ASUSE 2023–24; MSME Annual Report 2025–26. Trade includes wholesale + retail.",
);

// Explain the actual Sarvam-backed product pipeline rather than presenting
// generic AI claims.
setText("sh/3ah8rqlg", "Sarvam turns Indian speech into grounded business actions.");
setText("sh/obq90bml", "23");
setText("sh/pcjqtg36", "STT languages");
setText("sh/n6ls3alk", "11");
setText("sh/n2l4fq98", "TTS languages");
setText("sh/943mhgre", "23");
setText("sh/83ulovat", "document languages");
setText("sh/x8nml0ra", "THE LIVE VOICE LOOP");
setText(
  "sh/w7ulsvqp",
  "Saaras v3: speech → text\n\nSamvaad: context + tool calls\n\nBulbul v3: text → speech\n\nBarge-in for natural turns",
);
setText("sh/ja54na9g", "WHY IT FITS INDIA");
setText(
  "sh/i9w3u58v",
  "Accents + code-mixing\n\nAutomatic language detection\n\nEnglish-normalised tool inputs\n\nVision: invoice → reviewed stock",
);
setText(
  "sh/e10f2twf",
  "Result: owners speak naturally; Chhotu writes only through validated business APIs.",
);

deck.resolve("sl/hwbqtkby").speakerNotes.textFrame.setText(
  "Speaker 1: Our first market is not retail alone. MoSPI estimates 2.28 crore unincorporated trade establishments in 2023–24; trade covers wholesale and retail. Only 34.7% of trade establishments used the internet for entrepreneurial work. The Rs 55,244.31 crore figure is not the whole receivables market: it is the value of formal delayed-payment applications filed by micro and small enterprises on MSME Samadhaan through 31 December 2025. Together, the numbers show a very large operating base, uneven digitisation, and a costly credit problem.\n\n[Sources]\n- https://mospi.gov.in/sites/default/files/publication_reports/ASUSE_2023_24_Full_Report-L.pdf\n- https://msme.gov.in/sites/default/files/MSMEANNUALREPORT2025-26ENGLISH_0.pdf\n[/Sources]",
);
deck.resolve("sl/ofy9wn61").speakerNotes.textFrame.setText(
  "Speaker 2: Sarvam is the interaction layer we actually use. Saaras v3 converts multilingual and code-mixed speech into text. Samvaad carries the live conversation, interruptions, context, and grounded calls to our inventory, CRM, billing, and reporting tools. Bulbul v3 speaks the verified result back to the owner. In the fallback path, Chhotu.ai calls Saaras and Bulbul directly. For supplier documents, Sarvam Vision digitises images and PDFs; our backend then maps product, quantity, GST, freight, and cost to the shop catalogue and keeps a review gate before stock changes. We do not use a separate generic translation service in the live voice path: code-mixed transcription, transliteration, and English-normalised tool parameters keep backend actions reliable.\n\n[Sources]\n- https://docs.sarvam.ai/api/getting-started/models\n- https://docs.sarvam.ai/api/getting-started/models/saaras\n- https://docs.sarvam.ai/api/getting-started/models/sarvam-vision\n- https://docs.sarvam.ai/api/api-guides-tutorials/document-digitization/overview\n- https://www.sarvam.ai/products/conversational-agents\n- /Users/faizansheikh/ChhotuAI/backend/sarvam_client.py\n- /Users/faizansheikh/ChhotuAI/backend/samvaad_runtime.py\n- /Users/faizansheikh/ChhotuAI/frontend/src/samvaad.js\n[/Sources]",
);

const snapshot = await deck.inspect({
  kind: "slide,textbox,shape,notes",
  include: "id,slide,text,textPreview,textChars,textLines,bbox,bboxUnit",
  maxChars: 50000,
});
await fs.writeFile(
  "/Users/faizansheikh/ChhotuAI/.tmp/pitch-stack-revision/updated-inspect.ndjson",
  snapshot.ndjson,
);
const pptx = await PresentationFile.exportPptx(deck);
await pptx.save(output);
