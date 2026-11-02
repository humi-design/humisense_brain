/** Shared renderers for the four states every data view must handle. */

import type { ReactNode } from "react";
import { Alert, Spinner } from "./ui";
import type { ApiError } from "../api/client";

export function AsyncBoundary<T>({
  loading,
  error,
  data,
  onRetry,
  empty,
  children,
}: {
  loading: boolean;
  error: ApiError | null;
  data: T | null;
  onRetry: () => void;
  empty?: (data: T) => ReactNode;
  children: (data: T) => ReactNode;
}) {
  if (loading && data === null) {
    return <Spinner />;
  }

  if (error) {
    if (error.isUnauthenticated) {
      return (
        <Alert tone="warning" title="Your session has ended">
          Sign in again to continue.
        </Alert>
      );
    }
    if (error.isForbidden) {
      return (
        <Alert tone="warning" title="Not authorized">
          Your role does not permit this view. Nothing was changed.
        </Alert>
      );
    }
    if (error.isNotFound) {
      return (
        <Alert tone="warning" title="Not found">
          The requested resource does not exist in your organization.
        </Alert>
      );
    }
    return (
      <Alert tone="error" title="The request failed">
        <p>{error.message}</p>
        {error.correlationId ? (
          <p className="mt-1 font-mono text-xs opacity-80">
            correlation id: {error.correlationId}
          </p>
        ) : null}
        <button
          type="button"
          onClick={onRetry}
          className="mt-2 rounded-md border border-current/40 px-2.5 py-1 text-xs font-medium hover:bg-current/10"
        >
          Try again
        </button>
      </Alert>
    );
  }

  if (data === null) {
    return null;
  }

  if (empty) {
    const emptyView = empty(data);
    if (emptyView !== null) {
      return <>{emptyView}</>;
    }
  }

  return <>{children(data)}</>;
}
