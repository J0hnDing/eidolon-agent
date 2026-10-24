import { Component, ReactNode } from "react";
import { Link } from "react-router-dom";

export class PageErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    if (!this.state.failed) return this.props.children;
    return <section className="page stack" role="alert"><h1>This page couldn’t open</h1><p className="muted">Reload to try again. Your saved conversations and settings are still available.</p><div className="button-row"><button type="button" onClick={() => window.location.reload()}>Reload page</button><Link className="button-link secondary" to="/assistant">Return to Assistant</Link></div></section>;
  }
}
