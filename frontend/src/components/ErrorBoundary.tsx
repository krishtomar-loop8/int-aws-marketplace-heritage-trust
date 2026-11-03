import { Component, type ErrorInfo, type ReactNode } from "react";

// App-wide render safety net. Without this, any exception thrown while rendering
// (e.g. a malformed result field) unmounts the entire React tree and leaves a
// blank page. This catches it and shows a calm, actionable fallback in the content
// area while the sidebar and navigation stay usable. Give it a `key` that changes
// per view (e.g. the active tab) so switching away and back remounts and resets it.

interface Props {
  children: ReactNode;
}
interface State {
  hasError: boolean;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Surface for debugging; the fallback (not a blank page) is the user-facing part.
    console.error("Render error caught by ErrorBoundary:", error, info.componentStack);
  }

  render(): ReactNode {
    if (this.state.hasError) {
      return (
        <div className="error-boundary" role="alert">
          <h2>This view couldn't be displayed</h2>
          <p>
            Something went wrong while rendering this content. Your data is safe — try switching
            to another tab and back, or reload the page. If it keeps happening, let the team know
            which matter you were looking at.
          </p>
          <button className="link-btn" onClick={() => window.location.reload()}>
            Reload
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
