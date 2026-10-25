import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { ArrowUp, Mic } from "lucide-react";

import { useDictation, type DictationError } from "../../hooks/useDictation";

const MAX_LENGTH = 2000;

const DICTATION_ERROR: Record<DictationError, string> = {
  denied: "Microphone access is blocked. Allow it in your browser settings to dictate.",
  unsupported: "This browser cannot dictate. Type your message instead.",
  failed: "Could not hear that. Try the microphone again.",
};

/** Append dictated text to what is already typed, on one line of spacing. */
function appendText(current: string, segment: string): string {
  if (!current) return segment;
  return `${current}${/\s$/.test(current) ? "" : " "}${segment}`;
}

/**
 * Stationary composer. A single bordered field with the send control docked
 * inside it, so the input reads as one object rather than a stack of boxes.
 */
export function Composer({
  onSend,
  sending,
  disabled,
  placeholder,
}: {
  onSend: (text: string) => void;
  sending: boolean;
  disabled: boolean;
  placeholder: string;
}) {
  const [value, setValue] = useState("");
  const [dictationError, setDictationError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  const dictation = useDictation({
    onText: (segment) => {
      setValue((current) => {
        const next = appendText(current, segment);
        return next.length > MAX_LENGTH ? next.slice(0, MAX_LENGTH) : next;
      });
      setDictationError(null);
    },
    onError: (error) => {
      if (error === "denied") setDictationError(DICTATION_ERROR.denied);
      else if (error === "unsupported") setDictationError(DICTATION_ERROR.unsupported);
      else setDictationError(DICTATION_ERROR.failed);
    },
    // The browser's own language, so it hears the patient, not the UI locale.
    lang: navigator.language,
    disabled: disabled || sending,
  });

  useEffect(() => {
    const element = textareaRef.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, 152)}px`;
  }, [value]);

  useEffect(() => {
    if (!sending && !disabled) {
      textareaRef.current?.focus();
    }
  }, [sending, disabled]);

  const canSend = value.trim().length > 0 && !sending && !disabled;

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    if (!canSend) return;
    // Send what was said, not what the recognizer was still guessing.
    dictation.stop();
    onSend(value);
    setValue("");
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
    // A typed Escape ends dictation the same way tapping the mic does.
    if (event.key === "Escape" && dictation.listening) {
      event.preventDefault();
      dictation.stop();
    }
  };

  return (
    <form className="composer" onSubmit={submit}>
      <label className="sr-only" htmlFor="chat-input">
        Message the front desk
      </label>
      <div className="composer__field">
        <textarea
          id="chat-input"
          ref={textareaRef}
          className="composer__input"
          rows={1}
          value={value}
          maxLength={MAX_LENGTH}
          placeholder={placeholder}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={handleKeyDown}
          disabled={disabled}
          aria-describedby={dictationError ? "chat-input-dictation-error" : undefined}
        />
        {dictation.supported && (
          <button
            type="button"
            className={`composer__mic${dictation.listening ? " composer__mic--on" : ""}`}
            onClick={dictation.toggle}
            disabled={disabled || sending}
            aria-pressed={dictation.listening}
            aria-label={dictation.listening ? "Stop dictating" : "Dictate a message"}
            title={dictation.listening ? "Stop dictating (Esc)" : "Dictate a message"}
          >
            <Mic size={16} strokeWidth={2} aria-hidden="true" />
          </button>
        )}
        <button
          type="submit"
          className="composer__send"
          disabled={!canSend}
          aria-label={sending ? "Sending message" : "Send message"}
        >
          {sending ? (
            <span className="composer__spinner" aria-hidden="true" />
          ) : (
            <ArrowUp size={16} strokeWidth={2.25} aria-hidden="true" />
          )}
        </button>
      </div>
      {dictation.listening && (
        <p className="composer__interim" role="status" aria-live="polite">
          <span className="composer__recording" aria-hidden="true" />
          Listening{dictation.interim ? `: ${dictation.interim}` : "…"}
        </p>
      )}
      {dictationError && (
        <p className="composer__error" id="chat-input-dictation-error" role="alert">
          {dictationError}
        </p>
      )}
    </form>
  );
}
