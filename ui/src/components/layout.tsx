import type { ReactNode } from "react";
import { useLanguage } from "../contexts/LanguageContext";
import SubscribePopup from "./SubscribePopup";
import constants from "@/constants";
import { NavLink } from "react-router-dom";
import { cn } from "@/lib/utils";

interface SiteLogoProps {
  size?: "sm" | "lg";
}

export function SiteLogo({ size = "sm" }: SiteLogoProps) {
  const sizeClass =
    size === "lg"
      ? "text-4xl font-extrabold leading-none tracking-tight md:text-5xl lg:text-6xl"
      : "text-[0.95rem] font-bold tracking-tight";
  const dotIndex = constants.APP_NAME.lastIndexOf(".");
  const base = dotIndex > 0 ? constants.APP_NAME.slice(0, dotIndex) : constants.APP_NAME;
  const tld = dotIndex > 0 ? constants.APP_NAME.slice(dotIndex) : "";
  return (
    <span dir="ltr" className="inline-flex">
      <span className={cn(sizeClass, "text-foreground")}>{base}</span>
      {tld && <span className={cn(sizeClass, "text-app-accent-red")}>{tld}</span>}
    </span>
  );
}

interface SiteHeaderProps {
  activePage?: string;
  children?: ReactNode;
  showNav?: boolean;
}

export function SiteHeader({ activePage, children, showNav = true }: SiteHeaderProps) {
  const { lang, setLang, t } = useLanguage();

  return (
    <nav className="ltr flex h-11 shrink-0 items-center gap-3 overflow-hidden border-b border-app-border bg-app-surface px-4">
      <a href="/" className="ltr flex shrink-0 items-center no-underline">
        <SiteLogo />
      </a>

      {/* Middle slot: flex spacer on plain pages, time filters + toggle on main page */}
      {children ?? <div className="flex-1" />}

      {showNav && (
        <>
          <NavLink
            to="/"
            end
            className={({ isActive }) =>
              cn(
                "shrink-0 rounded px-[0.45rem] py-[0.2rem] text-[0.8rem] font-medium no-underline transition-colors",
                isActive || activePage === "map"
                  ? "text-foreground"
                  : "text-muted-foreground hover:text-foreground",
              )
            }
          >
            {t.navMap}
          </NavLink>
          <NavLink
            to="/markets"
            className={({ isActive }) =>
              cn(
                "shrink-0 rounded px-[0.45rem] py-[0.2rem] text-[0.8rem] font-medium no-underline transition-colors",
                isActive || activePage === "markets"
                  ? "text-foreground"
                  : "text-muted-foreground hover:text-foreground",
              )
            }
          >
            {t.navMarkets}
          </NavLink>
          <NavLink
            to="/newsletter"
            className={({ isActive }) =>
              cn(
                "shrink-0 rounded px-[0.45rem] py-[0.2rem] text-[0.8rem] font-medium no-underline transition-colors",
                isActive || activePage === "newsletter"
                  ? "text-foreground"
                  : "text-muted-foreground hover:text-foreground",
              )
            }
          >
            {t.newsletter}
          </NavLink>
          <NavLink
            to="/about"
            className={({ isActive }) =>
              cn(
                "shrink-0 rounded px-[0.45rem] py-[0.2rem] text-[0.8rem] font-medium no-underline transition-colors",
                isActive || activePage === "about" ? "text-foreground" : "text-muted-foreground hover:text-foreground",
              )
            }
          >
            {t.about}
          </NavLink>
        </>
      )}

      <button
        onClick={() => setLang(lang === "en" ? "ar" : "en")}
        title={lang === "en" ? t.switchToArabic : t.switchToEnglish}
        className="shrink-0 cursor-pointer rounded px-[0.45rem] py-[0.2rem] text-[0.8rem] font-medium text-muted-foreground transition-colors hover:text-foreground"
      >
        {lang === "en" ? "ع" : "EN"}
      </button>

      {showNav && <SubscribePopup />}

      <span className="shrink-0 font-mono text-[0.7rem] text-muted-foreground/50">v{constants.VERSION}</span>
    </nav>
  );
}

export function SiteFooter() {
  const { t } = useLanguage();
  const links = [
    { href: "/terms", label: t.termsLink },
    { href: "/privacy", label: t.privacyLink },
    { href: "/about", label: t.about },
  ];

  return (
    <footer className="flex flex-wrap gap-5 border-t border-app-border-mid px-8 py-5 text-[0.78rem] text-app-text-ghost no-underline">
      {links.map(({ href, label }) => (
        <NavLink
          key={href}
          to={href}
          className={({ isActive }) =>
            `transition-colors hover:text-foreground ${isActive ? "font-medium text-foreground" : ""}`
          }
        >
          {label}
        </NavLink>
      ))}
    </footer>
  );
}

interface PageLayoutProps {
  children: ReactNode;
  activePage?: string;
}

export function PageLayout({ children, activePage }: PageLayoutProps) {
  return (
    <div className="min-h-screen bg-app-bg text-app-text-primary">
      <SiteHeader activePage={activePage} />
      <main className="mx-auto max-w-xl flex-1">{children}</main>
      <SiteFooter />
    </div>
  );
}
