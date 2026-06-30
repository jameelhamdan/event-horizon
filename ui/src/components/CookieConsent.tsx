"use client";

import { useLanguage } from "@/contexts/LanguageContext";
import { useState, useEffect } from "react";

const GA_ID = import.meta.env.VITE_GA_ID as string | undefined;
const CONSENT_KEY = "eh_cookie_consent";

declare global {
  interface Window {
    dataLayer: unknown[];
    gtag: (...args: unknown[]) => void;
  }
}

function loadGA(id: string) {
  if (!id || document.querySelector(`script[data-ga="${id}"]`)) return;
  window.dataLayer = window.dataLayer || [];
  window.gtag = function (...args: unknown[]) {
    window.dataLayer.push(args);
  };
  window.gtag("js", new Date());
  window.gtag("config", id);
  const s = document.createElement("script");
  s.async = true;
  s.src = `https://www.googletagmanager.com/gtag/js?id=${id}`;
  s.dataset.ga = id;
  document.head.appendChild(s);
}

export default function CookieConsent() {
  const { t } = useLanguage();

  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const stored = localStorage.getItem(CONSENT_KEY);
    if (stored === "accepted" && GA_ID) {
      loadGA(GA_ID);
    } else if (!stored) {
      setVisible(true);
    }
  }, []);

  function accept() {
    localStorage.setItem(CONSENT_KEY, "accepted");
    if (GA_ID) loadGA(GA_ID);
    setVisible(false);
  }

  function decline() {
    localStorage.setItem(CONSENT_KEY, "declined");
    setVisible(false);
  }

  if (!visible) return null;

  return (
    <div className="cookie-banner">
      <p className="m-0 min-w-[220px] flex-1 text-[0.82rem] leading-[1.55] text-app-text-secondary">
        {t.cookies.message}{" "}
        <a href="/privacy" className="text-app-accent-blue no-underline">
          {t.privacyLink}
        </a>
      </p>
      <div className="flex shrink-0 gap-2">
        <button onClick={decline} className="cookie-btn-decline">
          {t.cookies.decline}
        </button>
        <button onClick={accept} className="cookie-btn-accept">
          {t.cookies.accept}
        </button>
      </div>
    </div>
  );
}
