// src/content/shared/NLCommandDetector.ts
//
// Phase 32 — Natural-language command interceptor (Hard Rule 66).
//
// Detects command-like phrases in the user's textarea before submission.
// Intercepts; does NOT forward to the AI; requires explicit user confirmation
// before executing any destructive action.
//
// Hard Rule 66: every destructive command requires explicit user confirmation.
// No one-click delete. No keyboard-shortcut delete without confirmation.

export interface NLCommand {
  id: string;
  patterns: RegExp[];
  destructive: boolean;
  confirmationLevel: 'single' | 'double';  // 'double' for bulk wipes
  description: string;
  execute(context: CommandContext): Promise<void>;
}

export interface CommandContext {
  chatSessionId: string | null;
  providerId: string;
  rawInput: string;
}

const BASE_URL = 'http://localhost:8000';

const COMMANDS: NLCommand[] = [
  {
    id: 'delete_this_chat',
    patterns: [
      /^\s*delete\s+this\s+chat\s*(history)?\s*\.?\s*$/i,
      /^\s*wipe\s+memory\s+for\s+this\s+chat\s*\.?\s*$/i,
      /^\s*forget\s+this\s+conversation\s*\.?\s*$/i,
      /^\s*clear\s+memory\s+layer\s+for\s+this\s+chat\s*\.?\s*$/i,
    ],
    destructive: true,
    confirmationLevel: 'single',
    description: 'Delete all MemStrata data for this chat session',
    async execute(ctx) {
      if (!ctx.chatSessionId) return;
      await fetch(`${BASE_URL}/chat-session/delete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          chat_session_id: ctx.chatSessionId,
          provider_id: ctx.providerId,
        }),
      });
    },
  },
  {
    id: 'delete_all_memory',
    patterns: [
      /^\s*delete\s+all\s+(my\s+)?memory(\s+layer)?(\s+data)?\s*\.?\s*$/i,
      /^\s*wipe\s+all\s+memory\s+layer\s+data\s*\.?\s*$/i,
      /^\s*nuke\s+everything\s*\.?\s*$/i,
    ],
    destructive: true,
    confirmationLevel: 'double',
    description: 'Delete ALL MemStrata data across all chats and providers',
    async execute(_ctx) {
      await fetch(`${BASE_URL}/memory/delete-all`, { method: 'POST' });
    },
  },
  {
    id: 'pause',
    patterns: [
      /^\s*pause\s+memory\s+layer\s*\.?\s*$/i,
      /^\s*stop\s+tracking\s*\.?\s*$/i,
      /^\s*disable\s+memory\s+layer\s*\.?\s*$/i,
    ],
    destructive: false,
    confirmationLevel: 'single',
    description: 'Disable MemStrata for the rest of this browser session',
    async execute(_ctx) {
      if (typeof chrome !== 'undefined' && chrome.storage?.session) {
        await chrome.storage.session.set({ memoryLayerPaused: true });
      }
    },
  },
  {
    id: 'show_memory',
    patterns: [
      /^\s*what\s+do\s+you\s+remember\s*\??\s*$/i,
      /^\s*show\s+memory\s*\.?\s*$/i,
      /^\s*show\s+me\s+what\s+you('?ve)?\s+saved\s*\.?\s*$/i,
    ],
    destructive: false,
    confirmationLevel: 'single',
    description: 'Open the MemStrata side panel showing saved context',
    async execute(_ctx) {
      if (typeof chrome !== 'undefined' && chrome.runtime) {
        chrome.runtime.sendMessage({ type: 'open_side_panel' });
      }
    },
  },
];

export class NLCommandDetector {
  /**
   * Returns a matching command or null. Caller is responsible for showing
   * confirmation UI before invoking execute(). Hard Rule 66 — no auto-exec.
   */
  detect(text: string): NLCommand | null {
    for (const cmd of COMMANDS) {
      for (const pattern of cmd.patterns) {
        if (pattern.test(text)) return cmd;
      }
    }
    return null;
  }
}

export { COMMANDS };
