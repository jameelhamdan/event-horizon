import constants from "@/constants"

const NEWSLETTER_URL = `${constants.API_BASE}/newsletter`

export interface NewsletterArticle {
  id: string
  title: string
  source_url: string
  source_code: string
  category: string | null
  published_on: string
  banner_image_url: string | null
  event_intensity: number | null
}

export interface NewsletterSummary {
  id: string
  date: string
  subject: string
  sent_at: string | null
  event_count: number
  status: string
}

export interface NewsletterDetail extends NewsletterSummary {
  body: string
  generated_at: string
  sent_count: number
  articles: NewsletterArticle[]
  cover_image_url: string | null
  cover_image_credit: string | null
}

export interface NewslettersResponse {
  results: NewsletterSummary[]
  count: number
}

export async function fetchNewsletters(): Promise<NewslettersResponse> {
  const res = await fetch(`${NEWSLETTER_URL}/`)
  if (!res.ok) throw new Error(`Failed to fetch newsletters: ${res.status}`)
  return res.json()
}

export async function fetchNewsletter(date: string): Promise<NewsletterDetail> {
  const res = await fetch(`${NEWSLETTER_URL}/${date}/`)
  if (!res.ok) throw new Error(`Newsletter not found: ${res.status}`)
  return res.json()
}

export async function fetchLatestNewsletter(): Promise<NewsletterDetail> {
  const res = await fetch(`${NEWSLETTER_URL}/latest/`)
  if (!res.ok) throw new Error(`No newsletter available: ${res.status}`)
  return res.json()
}

export async function subscribeToNewsletter(
  email: string
): Promise<{ detail: string }> {
  const res = await fetch(`${NEWSLETTER_URL}/subscribe/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  })
  const data = await res.json()
  if (!res.ok) {
    const msg = data?.email?.[0] ?? data?.detail ?? "Subscription failed."
    throw new Error(msg)
  }
  return data
}
