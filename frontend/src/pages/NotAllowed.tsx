import { Link } from "react-router-dom";
import { EmptyState, Page } from "../components/ui";

export default function NotAllowed({ notFound = false }: { notFound?: boolean }) {
  return (
    <Page>
      <div className="card mt-10">
        <EmptyState
          title={notFound ? "Page not found" : "You do not have access to this area"}
          description={
            notFound
              ? "The page you are looking for does not exist or has moved."
              : "Your role does not include this service. Ask an administrator if you need access."
          }
          action={
            <Link to="/" className="btn-secondary">
              Back to overview
            </Link>
          }
        />
      </div>
    </Page>
  );
}
