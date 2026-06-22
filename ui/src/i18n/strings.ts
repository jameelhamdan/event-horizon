import type { Language } from "../contexts/LanguageContext"
import constants from "@/constants"

export interface UIStrings {
  about: string
  newsletter: string
  subscribe: string
  filter6h: string
  filter24h: string
  filter7d: string
  filter30d: string
  events: string
  loading: string
  noEvents: string
  showArticles: string
  hideArticles: string
  mapTab: string
  listTab: string
  briefingsTab: string
  tabMarkets: string
  tabForecasts: string
  tabEvents: string
  dateFrom: string
  dateTo: string
  clearDateRange: string
  topicHistoryTitle: string
  topicHistoryEmpty: string
  allMonths: string
  notams: string
  earthquakes: string
  locations: string
  minutesAgo: (n: number) => string
  hoursAgo: (n: number) => string
  daysAgo: (n: number) => string
  justNow: string
  articleCount: (n: number, sources?: string[]) => string
  eventCount: (n: number) => string
  subscribeTitle: string
  subscribeTagline: string
  checkEmailConfirm: string
  subscribingLabel: string
  markets: string
  symbolCol: string
  priceCol: string
  changeCol: string
  noDataYet: string
  streamKeys: {
    stock: string
    crypto: string
    commodity: string
    forex: string
    bond: string
    index: string
  }
  pastBriefings: string
  selectBriefing: string
  noBriefingsYet: string
  back: string
  imageCredit: string
  openSourceRealtime: string
  aboutHeroTagline: string
  aboutWhatWeDoTitle: string
  aboutWhatWeDo: string
  aboutLegendTitle: string
  aboutContactTitle: string
  aboutContactFooter: string
  contactLabelGeneral: string
  contactLabelData: string
  contactLabelPress: string
  contactNoteData: string
  contactNotePress: string
  categoryDescs: Record<string, string>
  privacyPageTitle: string
  termsPageTitle: string
  lastUpdated: string
  termsLink: string
  privacyLink: string
  cookies: {
    accept: string
    decline: string
    message: string
  }
  // Controls / tooltips
  hideSidebar: string
  showSidebar: string
  clearTimeFilter: string
  clearTopicFilter: string
  switchToArabic: string
  switchToEnglish: string
  noEventsFiltered: string
  // Forecast panel (event-fused symbol prediction)
  marketForecasts: string
  noForecasts: string
  forecastPlaceholderNote: string
  forecastNote: string
  forecastHorizon1d: string
  forecastHorizon5d: string
  forecastProbUp: string
  forecastAccuracy: string
  forecastNoModel: string
  forecastProjection: string
  forecastHistory: string
  // Nav + Markets page
  navMap: string
  navMarkets: string
  eventsImpactHeatmap: string
  mostImpacted: string
  heatmapNet: string
  heatmapEmpty: string
  heatmapUp: string
  heatmapDown: string
  eventCountLabel: string
  // Indicator relationship visualisations (Markets page)
  indicatorsCompare: string
  indicatorsCompareNote: string
  causeEffectTitle: string
  causeEffectNote: string
  causeEffectEmpty: string
  causeLabel: string
  effectLabel: string
  // Price chart
  priceVolume: string
  priceNoHistory: string
  sentBullish: string
  sentBearish: string
  sentNeutral: string
  affectedIndicators: string
  magnitudeBuckets: Record<string, string>
  volatilityBuckets: Record<string, string>
  reliabilityLabels: Record<string, string>
  // Map — static point labels
  pointTypeLabels: Record<string, string>
  mapTimezone: string
  mapCurrency: string
  mapWebsite: string
  mapProducts: string
  mapPortType: string
  mapTeuRankLabel: string
  mapTeuRank: (rank: string | number) => string
  // Map overlays
  notamUntil: string
  earthquakeTsunami: string
  earthquakeDepth: string
  // Newsletter
  invalidDateInUrl: string
  couldNotLoadNewsletter: string
  // Confirm page
  confirmSubscriptionTitle: string
  confirmingSubscription: string
  invalidConfirmLink: string
  subscriptionConfirmed: string
  youreSubscribed: string
  confirmationFailed: string
  goToLiveMap: string
  backToLiveMap: string
  somethingWentWrong: string
  // Unsubscribe page
  unsubscribeTitle: string
  processingUnsubscribe: string
  alreadyUnsubscribed: string
  unsubscribed: string
  notOnMailingList: string
  removedFromList: string
  invalidLink: string
  invalidUnsubscribeLink: string
}

export const UI: Record<Language, UIStrings> = {
  en: {
    about: "About",
    newsletter: "Newsletter",
    subscribe: "Subscribe",
    filter6h: "6h",
    filter24h: "24h",
    filter7d: "7d",
    filter30d: "30d",
    events: "events",
    loading: "Loading…",
    noEvents: "No events found.",
    showArticles: "Show articles ▾",
    hideArticles: "Hide articles ▴",
    mapTab: "Map",
    listTab: "Events",
    briefingsTab: "Briefings",
    tabMarkets: "Markets",
    tabForecasts: "Forecasts",
    tabEvents: "Events",
    dateFrom: "From date",
    dateTo: "To date",
    clearDateRange: "Clear date range",
    topicHistoryTitle: "Past topics",
    topicHistoryEmpty: "No topics for this period",
    allMonths: "All months",
    notams: "NOTAMs",
    earthquakes: "Earthquakes",
    locations: "Locations",
    minutesAgo: (n) => `${n}m ago`,
    hoursAgo: (n) => `${n}h ago`,
    daysAgo: (n) => `${n}d ago`,
    justNow: "just now",
    articleCount: (n, sources) => {
      if (sources && sources.length > 0) {
        const first = sources[0]
        const rest = n - 1
        return rest > 0 ? `${first} and ${rest} more` : first
      }
      return `${n} article${n !== 1 ? "s" : ""}`
    },
    eventCount: (n) => `${n} event${n !== 1 ? "s" : ""}`,
    subscribeTitle: "Daily Briefings",
    subscribeTagline: "Get the day's top conflict intelligence in your inbox.",
    checkEmailConfirm: "Check your email to confirm.",
    subscribingLabel: "Subscribing…",
    markets: "Markets",
    symbolCol: "Symbol",
    priceCol: "Price",
    changeCol: "Chg%",
    noDataYet: "No data yet",
    streamKeys: {
      stock: "Stocks",
      crypto: "Crypto",
      commodity: "Commodities",
      forex: "Forex",
      bond: "Bonds",
      index: "Indices",
    },
    pastBriefings: "Past briefings",
    selectBriefing: "Select a briefing on the left to read.",
    noBriefingsYet: "No newsletters published yet.",
    back: "← Back",
    imageCredit: "Image:",
    openSourceRealtime: "Open-source · Real-time",
    aboutHeroTagline:
      "A real-time intelligence platform that turns raw news into a global conflict picture.",
    aboutWhatWeDoTitle: "What we do",
    aboutWhatWeDo:
      `${constants.APP_NAME} monitors hundreds of news sources — wire feeds, RSS, and regional outlets — and uses natural language processing to extract, classify, and geolocate events as they happen. The result is an interactive live map where you can explore ongoing conflicts, protests, disasters, and political developments anywhere in the world.`,
    aboutLegendTitle: "Category legend",
    aboutContactTitle: "Contact",
    aboutContactFooter:
      `${constants.APP_NAME} is a small independent project. We aim to respond within 48 hours but cannot guarantee replies to every message. For urgent operational matters, include "URGENT" in the subject line.`,
    contactLabelGeneral: "General enquiries",
    contactLabelData: "Source & data requests",
    contactLabelPress: "Press & media",
    contactNoteData:
      "Want us to track a specific region, outlet, or news feed? Send us the details.",
    contactNotePress:
      "For media use of our data or map embeds, please reach out before publishing.",
    categoryDescs: {
      conflict: "Armed clashes, military operations, airstrikes",
      protest: "Demonstrations, civil unrest, strikes",
      disaster: "Natural disasters, industrial accidents",
      political: "Elections, diplomacy, government decisions",
      economic: "Sanctions, market events, trade disruptions",
      crime: "High-profile crime, organized crime, arrests",
      general: "Other noteworthy events",
    },
    privacyPageTitle: "Privacy Policy",
    termsPageTitle: "Terms of Service",
    lastUpdated: "Last updated:",
    termsLink: "Terms of Service",
    privacyLink: "Privacy Policy",
    cookies: {
      accept: "Accept cookies",
      decline: "Decline",
      message:
        "We use cookies to analyse site traffic via Google Analytics. No personal data is collected without your consent.",
    },
    hideSidebar: "Hide sidebar",
    showSidebar: "Show sidebar",
    clearTimeFilter: "Clear time filter",
    clearTopicFilter: "Clear topic filter",
    switchToArabic: "Switch to Arabic",
    switchToEnglish: "Switch to English",
    noEventsFiltered: "No events match the current filters.",
    sentBullish: "bullish",
    sentBearish: "bearish",
    sentNeutral: "neutral",
    marketForecasts: "Market Forecasts",
    noForecasts: "No forecasts available",
    forecastPlaceholderNote: "Event-fused predictions — direction from news + price history.",
    forecastNote: "Event-fused predictions — direction from news + price history.",
    forecastHorizon1d: "1 day",
    forecastHorizon5d: "5 days",
    forecastProbUp: "P(up)",
    forecastAccuracy: "Accuracy",
    forecastNoModel: "No forecasts yet — backfill prices and train the model.",
    forecastProjection: "Projection",
    forecastHistory: "History",
    navMap: "Map",
    navMarkets: "Markets",
    eventsImpactHeatmap: "Event Impact on Markets",
    mostImpacted: "Most-impacted indicators",
    heatmapNet: "Net event pressure by category",
    heatmapEmpty: "No weighted events in this window",
    heatmapUp: "upward pressure",
    heatmapDown: "downward pressure",
    eventCountLabel: "events",
    indicatorsCompare: "Indicator performance",
    indicatorsCompareNote: "Daily close, rebased to 0% at window start — shows how indicators move against each other.",
    causeEffectTitle: "Cause → effect graph",
    causeEffectNote: "How recent event categories press on each market indicator. Edge thickness = strength, colour = direction.",
    causeEffectEmpty: "No weighted events to map in this window",
    causeLabel: "Causes",
    effectLabel: "Effects",
    priceVolume: "Volume",
    priceNoHistory: "No price history",
    affectedIndicators: "Affected indicators",
    magnitudeBuckets: {
      strong_down: "Strong ↓",
      down: "Down",
      flat: "Flat",
      up: "Up",
      strong_up: "Strong ↑",
    },
    volatilityBuckets: {
      calm: "Calm",
      normal: "Normal",
      elevated: "Elevated",
    },
    reliabilityLabels: {
      high: "High",
      med: "Med",
      low: "Low",
    },
    pointTypeLabels: {
      exchange: "Stock Exchange",
      commodity_exchange: "Commodity Exchange",
      port: "Major Port",
      central_bank: "Central Bank",
    },
    mapTimezone: "Timezone",
    mapCurrency: "Currency",
    mapWebsite: "Website",
    mapProducts: "Products",
    mapPortType: "Port type",
    mapTeuRankLabel: "TEU rank",
    mapTeuRank: (rank) => `#${rank} globally`,
    notamUntil: "Until",
    earthquakeTsunami: "TSUNAMI",
    earthquakeDepth: "Depth:",
    invalidDateInUrl: "Invalid date in URL.",
    couldNotLoadNewsletter: "Could not load newsletter.",
    confirmSubscriptionTitle: "Confirm subscription",
    confirmingSubscription: "Confirming your subscription…",
    invalidConfirmLink: "Invalid confirmation link.",
    subscriptionConfirmed: "Subscription confirmed!",
    youreSubscribed: "You're subscribed",
    confirmationFailed: "Confirmation failed",
    goToLiveMap: "Go to live map",
    backToLiveMap: "← Live map",
    somethingWentWrong: "Something went wrong. Please try again later.",
    unsubscribeTitle: "Unsubscribe",
    processingUnsubscribe: "Processing…",
    alreadyUnsubscribed: "Already unsubscribed",
    unsubscribed: "Unsubscribed",
    notOnMailingList: "This email is not on our mailing list.",
    removedFromList: "You've been removed from the daily briefing list. You won't receive any more emails from us.",
    invalidLink: "Invalid link",
    invalidUnsubscribeLink: "Invalid unsubscribe link.",
  },
  ar: {
    about: "حول",
    newsletter: "النشرة",
    subscribe: "اشتراك",
    filter6h: "٦س",
    filter24h: "٢٤س",
    filter7d: "٧أ",
    filter30d: "٣٠أ",
    events: "أحداث",
    loading: "جارٍ التحميل…",
    noEvents: "لا توجد أحداث.",
    showArticles: "عرض المقالات ▾",
    hideArticles: "إخفاء المقالات ▴",
    mapTab: "الخريطة",
    listTab: "الأحداث",
    briefingsTab: "النشرات",
    tabMarkets: "الأسواق",
    tabForecasts: "التوقعات",
    tabEvents: "الأحداث",
    dateFrom: "من تاريخ",
    dateTo: "إلى تاريخ",
    clearDateRange: "مسح النطاق الزمني",
    topicHistoryTitle: "مواضيع سابقة",
    topicHistoryEmpty: "لا مواضيع لهذه الفترة",
    allMonths: "كل الأشهر",
    notams: "نوتام",
    earthquakes: "زلازل",
    locations: "مواقع",
    minutesAgo: (n) => `منذ ${n} د`,
    hoursAgo: (n) => `منذ ${n} س`,
    daysAgo: (n) => `منذ ${n} ي`,
    justNow: "الآن",
    articleCount: (n, sources) => {
      if (sources && sources.length > 0) {
        const first = sources[0]
        const rest = n - 1
        return rest > 0 ? `${first} و${rest} آخرين` : first
      }
      return `${n} مقال${n !== 1 ? "ات" : ""}`
    },
    eventCount: (n) => `${n} حدث${n !== 1 ? " ًا" : ""}`,
    subscribeTitle: "النشرات اليومية",
    subscribeTagline:
      "احصل على أبرز أخبار النزاعات في بريدك الإلكتروني يومياً.",
    checkEmailConfirm: "تحقق من بريدك الإلكتروني للتأكيد.",
    subscribingLabel: "جارٍ الاشتراك…",
    markets: "الأسواق",
    symbolCol: "رمز",
    priceCol: "السعر",
    changeCol: "تغيير%",
    noDataYet: "لا بيانات بعد",
    streamKeys: {
      stock: "أسهم",
      crypto: "كريبتو",
      commodity: "سلع",
      forex: "فوركس",
      bond: "سندات",
      index: "مؤشرات",
    },
    pastBriefings: "النشرات السابقة",
    selectBriefing: "اختر نشرة من القائمة للقراءة.",
    noBriefingsYet: "لا توجد نشرات منشورة بعد.",
    back: "رجوع",
    imageCredit: "الصورة:",
    openSourceRealtime: "مفتوح المصدر · مباشر",
    aboutHeroTagline:
      "منصة استخبارات آنية تحول الأخبار الخام إلى صورة شاملة للنزاعات العالمية.",
    aboutWhatWeDoTitle: "ما نفعله",
    aboutWhatWeDo:
      `ترصد ${constants.APP_NAME} مئات المصادر الإخبارية — وكالات الأنباء وخلاصات RSS والمنافذ الإقليمية — وتستخدم معالجة اللغة الطبيعية لاستخلاص الأحداث وتصنيفها وتحديد مواقعها الجغرافية فور وقوعها. والنتيجة خريطة تفاعلية حية يمكنك من خلالها استكشاف النزاعات والاحتجاجات والكوارث والتطورات السياسية في أي مكان من العالم.`,
    aboutLegendTitle: "دليل التصنيفات",
    aboutContactTitle: "تواصل معنا",
    aboutContactFooter:
      `${constants.APP_NAME} مشروع مستقل صغير. نسعى للرد خلال ٤٨ ساعة، لكن لا نستطيع ضمان الرد على جميع الرسائل. للأمور العاجلة، يرجى كتابة «عاجل» في سطر الموضوع.`,
    contactLabelGeneral: "استفسارات عامة",
    contactLabelData: "طلبات المصادر والبيانات",
    contactLabelPress: "الإعلام والصحافة",
    contactNoteData:
      "هل تريد منا تتبع منطقة أو منفذ إعلامي أو خلاصة إخبارية معينة؟ أرسل لنا التفاصيل.",
    contactNotePress:
      "لاستخدام بياناتنا أو تضمين خرائطنا إعلامياً، يرجى التواصل معنا قبل النشر.",
    categoryDescs: {
      conflict: "اشتباكات مسلحة، عمليات عسكرية، غارات جوية",
      protest: "مظاهرات، اضطرابات مدنية، إضرابات",
      disaster: "كوارث طبيعية، حوادث صناعية",
      political: "انتخابات، دبلوماسية، قرارات حكومية",
      economic: "عقوبات، أحداث اقتصادية، اضطرابات تجارية",
      crime: "جرائم بارزة، جريمة منظمة، اعتقالات",
      general: "أحداث جديرة بالاهتمام",
    },
    privacyPageTitle: "سياسة الخصوصية",
    termsPageTitle: "شروط الخدمة",
    lastUpdated: "آخر تحديث:",
    termsLink: "شروط الخدمة",
    privacyLink: "سياسة الخصوصية",
    cookies: {
      accept: "قبول الكوكيز",
      decline: "رفض",
      message:
        "نحن نستخدم الكوكيز لتحليل حركة الموقع عبر Google Analytics. لا يتم جمع أي بيانات شخصية دون موافقتك.",
    },
    hideSidebar: "إخفاء الشريط الجانبي",
    showSidebar: "إظهار الشريط الجانبي",
    clearTimeFilter: "مسح فلتر الوقت",
    clearTopicFilter: "مسح فلتر الموضوع",
    switchToArabic: "التبديل إلى العربية",
    switchToEnglish: "التبديل إلى الإنجليزية",
    noEventsFiltered: "لا توجد أحداث تطابق الفلاتر الحالية.",
    sentBullish: "صعودي",
    sentBearish: "هبوطي",
    sentNeutral: "محايد",
    marketForecasts: "توقعات الأسواق",
    noForecasts: "لا توجد توقعات متاحة",
    forecastPlaceholderNote: "توقعات مدمجة بالأحداث — الاتجاه من الأخبار وتاريخ الأسعار.",
    forecastNote: "توقعات مدمجة بالأحداث — الاتجاه من الأخبار وتاريخ الأسعار.",
    forecastHorizon1d: "يوم واحد",
    forecastHorizon5d: "٥ أيام",
    forecastProbUp: "احتمال الصعود",
    forecastAccuracy: "الدقة",
    forecastNoModel: "لا توجد توقعات بعد — عبّئ الأسعار ودرّب النموذج.",
    forecastProjection: "الإسقاط",
    forecastHistory: "السجل",
    navMap: "الخريطة",
    navMarkets: "الأسواق",
    eventsImpactHeatmap: "تأثير الأحداث على الأسواق",
    mostImpacted: "المؤشرات الأكثر تأثراً",
    heatmapNet: "صافي ضغط الأحداث حسب الفئة",
    heatmapEmpty: "لا توجد أحداث مرجّحة في هذه الفترة",
    heatmapUp: "ضغط صعودي",
    heatmapDown: "ضغط هبوطي",
    eventCountLabel: "أحداث",
    indicatorsCompare: "أداء المؤشرات",
    indicatorsCompareNote: "الإغلاق اليومي، معاد ضبطه إلى ٪٠ في بداية الفترة — يوضح كيف تتحرك المؤشرات مقابل بعضها.",
    causeEffectTitle: "رسم السبب ← الأثر",
    causeEffectNote: "كيف تضغط فئات الأحداث الأخيرة على كل مؤشر سوقي. سماكة الخط = القوة، اللون = الاتجاه.",
    causeEffectEmpty: "لا توجد أحداث مرجّحة لرسمها في هذه الفترة",
    causeLabel: "الأسباب",
    effectLabel: "الآثار",
    priceVolume: "الحجم",
    priceNoHistory: "لا يوجد سجل أسعار",
    affectedIndicators: "المؤشرات المتأثرة",
    magnitudeBuckets: {
      strong_down: "هبوط قوي ↓",
      down: "هبوط",
      flat: "ثابت",
      up: "صعود",
      strong_up: "صعود قوي ↑",
    },
    volatilityBuckets: {
      calm: "هادئ",
      normal: "عادي",
      elevated: "مرتفع",
    },
    reliabilityLabels: {
      high: "عالية",
      med: "متوسطة",
      low: "منخفضة",
    },
    pointTypeLabels: {
      exchange: "بورصة الأسهم",
      commodity_exchange: "بورصة السلع",
      port: "ميناء رئيسي",
      central_bank: "بنك مركزي",
    },
    mapTimezone: "المنطقة الزمنية",
    mapCurrency: "العملة",
    mapWebsite: "الموقع",
    mapProducts: "المنتجات",
    mapPortType: "نوع الميناء",
    mapTeuRankLabel: "ترتيب TEU",
    mapTeuRank: (rank) => `عالمياً #${rank}`,
    notamUntil: "حتى",
    earthquakeTsunami: "تسونامي",
    earthquakeDepth: "العمق:",
    invalidDateInUrl: "تاريخ غير صحيح في الرابط.",
    couldNotLoadNewsletter: "تعذّر تحميل النشرة.",
    confirmSubscriptionTitle: "تأكيد الاشتراك",
    confirmingSubscription: "جارٍ تأكيد اشتراكك…",
    invalidConfirmLink: "رابط التأكيد غير صحيح.",
    subscriptionConfirmed: "تم تأكيد الاشتراك!",
    youreSubscribed: "أنت مشترك",
    confirmationFailed: "فشل التأكيد",
    goToLiveMap: "اذهب إلى الخريطة المباشرة",
    backToLiveMap: "الخريطة المباشرة",
    somethingWentWrong: "حدث خطأ ما. يرجى المحاولة لاحقاً.",
    unsubscribeTitle: "إلغاء الاشتراك",
    processingUnsubscribe: "جارٍ المعالجة…",
    alreadyUnsubscribed: "ألغيت اشتراكك مسبقاً",
    unsubscribed: "تم إلغاء الاشتراك",
    notOnMailingList: "هذا البريد الإلكتروني غير مشترك في قائمتنا.",
    removedFromList: "لقد تمت إزالتك من قائمة النشرات اليومية. لن تتلقى منا أي رسائل إلكترونية بعد الآن.",
    invalidLink: "رابط غير صحيح",
    invalidUnsubscribeLink: "رابط إلغاء الاشتراك غير صحيح.",
  },
}
