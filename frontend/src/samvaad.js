import {
  AgentState,
  BrowserAudioInterface,
  ConversationAgent,
  InteractionType,
} from "sarvam-conv-ai-sdk/browser";

// Keep the otherwise build-tool-free frontend simple: expose the small SDK
// surface used by index.html and serve this file as one versioned bundle.
window.ChhotuSamvaadSDK = {
  AgentState,
  BrowserAudioInterface,
  ConversationAgent,
  InteractionType,
};
