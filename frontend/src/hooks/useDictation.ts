/**
 * Voice input for the composer, on the browser's own speech recognizer.
 *
 * No dependency, no audio leaves the tab through this app: `SpeechRecognition`
 * streams the microphone to the browser vendor's service and hands back text.
 * Feature-detected, so a browser without it (Safari, Firefox today) simply
 * shows no microphone button instead of a control that fails on tap.
 *
 * Text is delivered as *segments* (`onText(segment)`) rather than as a whole
 * field value, so dictation appends to whatever the patient already typed and
 * the interims never fight a controlled input: the final result replaces its
 * own interim, and stopping mid-sentence keeps the words said so far.
 */
import { useCallback, useEffect, useRef, useState } from "react";

type SpeechAlternative = { transcript: string; confidence: number };
type SpeechResult = { isFinal: boolean; length: number; 0: SpeechAlternative };
type SpeechEvent = {
  resultIndex: number;
  results: { length: number; [index: number]: SpeechResult };
};
type SpeechErrorEvent = { error: string; message?: string };

type Recognition = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  maxAlternatives: number;
  start: () => void;
  stop: () => void;
  abort: () => void;
  onresult: ((event: SpeechEvent) => void) | null;
  onerror: ((event: SpeechErrorEvent) => void) | null;
  onend: (() => void) | null;
  onstart: (() => void) | null;
};

type RecognitionCtor = new () => Recognition;

/** Chrome/Edge expose it prefixed; the constructor is the feature check. */
function getRecognitionCtor(): RecognitionCtor | null {
  if (typeof window === "undefined") return null;
  const scope = window as unknown as {
    SpeechRecognition?: RecognitionCtor;
    webkitSpeechRecognition?: RecognitionCtor;
  };
  return scope.SpeechRecognition ?? scope.webkitSpeechRecognition ?? null;
}

export type DictationError = "denied" | "unsupported" | "failed";

type Options = {
  /** Called with each settled phrase, already space-trimmed. May be empty. */
  onText: (segment: string) => void;
  /** Silence turning the recognizer off, or a failure. */
  onError?: (error: DictationError) => void;
  /** The language to listen for, e.g. "en-US". Defaults to the page's. */
  lang?: string;
  /** Composer busy/sending: dictation is switched off while true. */
  disabled?: boolean;
};

/** Parse `error` codes into the three things the UI can actually say. */
function classify(error: string): DictationError {
  if (error === "not-allowed" || error === "service-not-allowed") return "denied";
  if (error === "language-not-supported") return "unsupported";
  return "failed";
}

export function useDictation({ onText, onError, lang, disabled = false }: Options) {
  // Resolved once: the constructor does not change during a page's lifetime.
  const [supported] = useState<boolean>(() => getRecognitionCtor() !== null);
  const [listening, setListening] = useState(false);
  // Interims are for showing, never for sending: they are dropped on stop.
  const [interim, setInterim] = useState("");

  const recognitionRef = useRef<Recognition | null>(null);
  // Latest callbacks, so the recognizer is built once and never re-wired.
  const onTextRef = useRef(onText);
  const onErrorRef = useRef(onError);
  onTextRef.current = onText;
  onErrorRef.current = onError;

  const stop = useCallback(() => {
    const recognition = recognitionRef.current;
    if (!recognition) return;
    // `stop` flushes the trailing interim as a final result; `abort` discards
    // it. Stopping deliberately keeps what the patient just said.
    recognition.stop();
  }, []);

  const start = useCallback(() => {
    if (recognitionRef.current) return;
    const Ctor = getRecognitionCtor();
    if (!Ctor) {
      onErrorRef.current?.("unsupported");
      return;
    }

    const recognition = new Ctor();
    recognition.lang = lang ?? document.documentElement.lang ?? "en-US";
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;

    recognition.onstart = () => {
      setListening(true);
      setInterim("");
    };
    recognition.onresult = (event) => {
      let settled = "";
      let pending = "";
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        const transcript = result[0]?.transcript ?? "";
        if (result.isFinal) settled += transcript;
        else pending += transcript;
      }
      if (settled.trim()) onTextRef.current(settled.trim());
      setInterim(pending);
    };
    recognition.onerror = (event) => {
      // "no-speech" and "aborted" are normal: silence, or our own stop().
      if (event.error === "no-speech" || event.error === "aborted") return;
      onErrorRef.current?.(classify(event.error));
    };
    recognition.onend = () => {
      // Chrome ends the session on its own after a pause even when
      // `continuous` is set. That is the stop we want, so drop the reference
      // and let the next tap start a fresh session.
      recognitionRef.current = null;
      setListening(false);
      setInterim("");
    };

    recognitionRef.current = recognition;
    try {
      recognition.start();
    } catch {
      // `start` throws if a previous session has not released the mic yet.
      recognitionRef.current = null;
      setListening(false);
      onErrorRef.current?.("failed");
    }
  }, [lang]);

  const toggle = useCallback(() => {
    if (disabled) return;
    if (recognitionRef.current) stop();
    else start();
  }, [disabled, start, stop]);

  // Never leave the microphone open behind a navigation or a disabled field.
  useEffect(() => {
    if (disabled && recognitionRef.current) {
      recognitionRef.current.abort();
      recognitionRef.current = null;
      setListening(false);
      setInterim("");
    }
  }, [disabled]);

  useEffect(
    () => () => {
      recognitionRef.current?.abort();
      recognitionRef.current = null;
    },
    [],
  );

  return { supported, listening, interim, start, stop, toggle };
}
