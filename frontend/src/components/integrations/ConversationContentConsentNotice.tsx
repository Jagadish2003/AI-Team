/**
 * ConversationContentConsentNotice — R18-A4 / AT-598 (T5, AC7)
 *
 * Connect-time consent copy for the DEPTH phase of the Slack (R16-A2) and Teams
 * (R17-A1) connectors. Reading conversation TEXT is more sensitive than reading
 * activity counts, so consent gets STRICTER at depth, not looser: the copy must
 * state plainly that message CONTENT in the in-scope channels is read and used as
 * discovery evidence a finding can cite (AC7) — not merely that channels are read.
 *
 * Shared by both platforms so the disclosure cannot drift between them — the two
 * connectors read content through the SAME substrate path (see
 * backend/discovery/ingest/conversation_content.py). `scopeLabel` names the
 * boundary each platform enforces: Slack's selected channels (P5), Teams' granted
 * channels. Private channels and DMs are never read on either platform.
 */
import React from 'react';
import { ShieldCheck } from 'lucide-react';

interface Props {
  /** The in-scope boundary, e.g. "selected channels" / "granted channels". */
  scopeLabel: string;
}

export default function ConversationContentConsentNotice({ scopeLabel }: Props) {
  return (
    <div
      role="note"
      aria-label="Conversation content consent"
      className="mt-3 flex gap-2 rounded-lg border border-accent/20 bg-accent/5 px-3 py-2.5"
    >
      <ShieldCheck size={14} className="mt-0.5 shrink-0 text-accent" aria-hidden />
      <p className="text-xs leading-relaxed text-text">
        Message content in {scopeLabel} is read and used as discovery evidence —
        thread text is indexed so findings can draw on and cite it. Private
        channels and direct messages are never read.
      </p>
    </div>
  );
}
