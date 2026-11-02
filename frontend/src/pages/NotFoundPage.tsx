import { Link } from "react-router-dom";
import { Compass } from "lucide-react";
import { EmptyState } from "../components/ui";

export function NotFoundPage() {
  return (
    <div className="p-8">
      <EmptyState
        icon={Compass}
        title="Page not found"
        description="The page you requested does not exist."
        action={
          <Link
            to="/"
            className="rounded-lg bg-accent px-3.5 py-2 text-sm font-medium text-canvas hover:bg-accent-strong"
          >
            Back to overview
          </Link>
        }
      />
    </div>
  );
}
