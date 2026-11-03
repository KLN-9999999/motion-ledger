import Link from "next/link";
import type { ReactNode } from "react";
export function Shell({children}:{children:ReactNode}) {
  return <div className="app-shell">
    <header className="topbar">
      <Link className="brand" href="/">
        <span className="brand-mark">ML</span><span><span className="brand-name">MotionLedger</span><span className="brand-tag">Process intelligence</span></span>
      </Link>
      <div className="top-actions"><span className="api-state"><i className="dot online"/> Local workspace</span><Link href="/procedures/new" className="btn small">New procedure</Link></div>
    </header>
    {children}
    <footer className="footer"><span>MotionLedger · AI that audits how work actually happens.</span><span>Process-focused · Evidence-linked · Human-reviewed</span></footer>
  </div>
}
