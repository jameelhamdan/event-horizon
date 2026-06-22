import "./index.css";

import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { LanguageProvider } from "@/contexts/LanguageContext";
import IndexPage from "@/pages/index";
import AboutPage from "@/pages/about";
import PrivacyPage from "@/pages/privacy";
import TermsPage from "@/pages/terms";
import NewsletterPage from "@/pages/newsletter/index";
import NewsletterDatePage from "@/pages/newsletter/detail";
import NewsletterConfirmPage from "@/pages/newsletter/confirm";
import NewsletterUnsubscribePage from "@/pages/newsletter/unsubscribe";
import CookieConsent from "@/components/CookieConsent";

document.documentElement.classList.add('dark')

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <LanguageProvider>
      <BrowserRouter>
        <CookieConsent />
        <Routes>
          <Route path="/" element={<IndexPage />} />
          <Route path="/about" element={<AboutPage />} />
          <Route path="/privacy" element={<PrivacyPage />} />
          <Route path="/terms" element={<TermsPage />} />
          <Route path="/contact" element={<Navigate to="/about" replace />} />
          <Route path="/newsletter" element={<NewsletterPage />} />
          <Route path="/newsletter/:year/:month/:day" element={<NewsletterDatePage />} />
          <Route path="/newsletter/confirm/:token" element={<NewsletterConfirmPage />} />
          <Route path="/newsletter/unsubscribe/:token" element={<NewsletterUnsubscribePage />} />
        </Routes>
      </BrowserRouter>
    </LanguageProvider>
  </React.StrictMode>,
);
