import { Link } from "react-router-dom";

export function NotFound({ what = "Page" }: { what?: string }) {
  return (
    <div className="card empty">
      <h2>{what} not found</h2>
      <p>It may have been deleted, or the link is wrong.</p>
      <Link className="btn" to="/">Back to worlds</Link>
    </div>
  );
}
