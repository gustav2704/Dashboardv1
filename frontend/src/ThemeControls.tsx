import { useEffect, useState } from 'react'
import { Monitor, Moon, Sun } from 'lucide-react'

export type ThemeChoice = 'system' | 'light' | 'dark'
const STORAGE_KEY = 'ea-theme'
export const THEME_EVENT = 'ea-themechange'

function readChoice(): ThemeChoice {
  try {
    const value = localStorage.getItem(STORAGE_KEY)
    return value === 'light' || value === 'system' ? value : 'dark'
  } catch {
    return 'dark'
  }
}

function applyChoice(choice: ThemeChoice) {
  const root = document.documentElement
  if (choice === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', choice)
  window.dispatchEvent(new Event(THEME_EVENT))
}

/** Reads a --tt-* token as a literal color, for libraries that cannot use CSS variables. */
export function themeColor(token: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(token).trim()
}

/** Calls `onChange` whenever the effective theme changes (toggle or OS setting). */
export function subscribeTheme(onChange: () => void): () => void {
  const media = window.matchMedia('(prefers-color-scheme: dark)')
  window.addEventListener(THEME_EVENT, onChange)
  media.addEventListener('change', onChange)
  return () => {
    window.removeEventListener(THEME_EVENT, onChange)
    media.removeEventListener('change', onChange)
  }
}

export function ThemeSwitch() {
  const [choice, setChoice] = useState<ThemeChoice>(readChoice)
  useEffect(() => {
    applyChoice(choice)
    try { localStorage.setItem(STORAGE_KEY, choice) } catch { /* storage unavailable */ }
  }, [choice])
  const options: Array<[ThemeChoice, string, typeof Sun]> = [['light', 'Light', Sun], ['dark', 'Dark', Moon], ['system', 'Auto', Monitor]]
  return <div className="theme-switch" role="group" aria-label="Theme">
    {options.map(([value, label, Icon]) => <button key={value} type="button" aria-pressed={choice === value} title={value === 'system' ? 'Follow the operating system' : `${label} theme`} onClick={() => setChoice(value)}><Icon size={13} aria-hidden="true" />{label}</button>)}
  </div>
}

/** Station clock: black hands, red second hand with the round disc. */
export function StationClock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000)
    return () => window.clearInterval(timer)
  }, [])
  const seconds = now.getSeconds()
  const minutes = now.getMinutes() + seconds / 60
  const hours = (now.getHours() % 12) + minutes / 60
  const label = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  return <svg className="station-clock" viewBox="0 0 100 100" role="img" aria-label={`Hora local ${label}`}>
    <title>{label}</title>
    <circle cx="50" cy="50" r="47" fill="var(--tt-clock-face)" stroke="var(--tt-rail)" strokeWidth="3" />
    {Array.from({ length: 12 }, (_, index) => <line key={index} x1="50" y1="7" x2="50" y2={index % 3 ? 14 : 19} stroke="var(--tt-rail)" strokeWidth={index % 3 ? 3 : 5} transform={`rotate(${index * 30} 50 50)`} />)}
    <line x1="50" y1="54" x2="50" y2="27" stroke="var(--tt-rail)" strokeWidth="7" transform={`rotate(${hours * 30} 50 50)`} />
    <line x1="50" y1="56" x2="50" y2="12" stroke="var(--tt-rail)" strokeWidth="5" transform={`rotate(${minutes * 6} 50 50)`} />
    <g transform={`rotate(${seconds * 6} 50 50)`}>
      <line x1="50" y1="64" x2="50" y2="22" stroke="#D52B1E" strokeWidth="2" />
      <circle cx="50" cy="21" r="6" fill="#D52B1E" />
    </g>
  </svg>
}
