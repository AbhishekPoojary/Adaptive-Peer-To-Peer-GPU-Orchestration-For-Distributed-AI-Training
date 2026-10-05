import { Link } from "react-router-dom";
import { useAuthProvidersQuery } from "@/api/auth";
import { useNodesQuery } from "@/api/nodes";
import { getUser, isAdmin } from "@/api/session";
import type { NodeSummary } from "@/api/types";
import { EnrollPanel } from "@/components/AddNodeModal";
import { ErrorState } from "@/components/ErrorState";
import { StatusPill } from "@/components/StatusPill";
import { Panel } from "@/components/ui/panel";
import { Skeleton } from "@/components/ui/skeleton";
import { formatBytes, formatRelativeTime } from "@/lib/format";
import { RemoveNodeButton } from "@/pages/NodesList";

/**
 * "Lend my computer": the join command, and the computers you have lent.
 *
 * Any signed-in user can get here and add their own machine without an admin
 * (ALLOW_SELF_LENDING). The command is minted for them, so the machine that
 * uses it is recorded as theirs — which is what fills "Your computers" and
 * lets them remove one again.
 */
export function Lend() {
  const providers = useAuthProvidersQuery();
  const nodes = useNodesQuery();
  const me = getUser();
  const refused = providers.data?.lending === false && !isAdmin();

  const mine = (nodes.data?.nodes ?? []).filter(
    (n) => me !== null && n.enrolled_by === me.username,
  );

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-4">
      <h1 className="text-[1.375rem] font-semibold tracking-[-0.02em] text-ink">
        Lend your computer
      </h1>

      {refused ? (
        <Panel>
          <h2 className="text-[0.9375rem] font-semibold text-ink">
            Only an admin can add computers here
          </h2>
          <p className="mt-1.5 text-[0.8125rem] text-muted">
            Whoever runs this site has chosen to approve each computer
            themselves. Ask the person who sent you the link to add yours.
          </p>
        </Panel>
      ) : (
        <Panel>
          <h2 className="text-[0.9375rem] font-semibold text-ink">
            Run this on the computer you want to lend
          </h2>
          <p className="mb-4 mt-1.5 text-[0.8125rem] leading-relaxed text-muted">
            It downloads what it needs, connects, and starts taking training
            jobs. You only do this once per computer: after that, running the
            same command again reconnects it as itself.
          </p>
          {nodes.isPending ? (
            <Skeleton className="h-24 w-full" />
          ) : (
            // Mounted once the list has loaded, so the machines already there
            // are the baseline and only a genuinely new one counts as "yours
            // just connected".
            <EnrollPanel existingNodes={nodes.data?.nodes ?? []} active />
          )}
        </Panel>
      )}

      <Panel>
        <h2 className="text-[0.9375rem] font-semibold text-ink">Your computers</h2>
        {nodes.isError ? (
          <div className="mt-3">
            <ErrorState error={nodes.error} onRetry={() => nodes.refetch()} />
          </div>
        ) : nodes.isPending ? (
          <Skeleton className="mt-3 h-12 w-full" />
        ) : mine.length === 0 ? (
          <p className="mt-1.5 text-[0.8125rem] text-muted">
            None yet. Once you run the command, your computer appears here.
          </p>
        ) : (
          <ul className="mt-2 divide-y divide-hairline">
            {mine.map((node) => (
              <MyComputer key={node.id} node={node} />
            ))}
          </ul>
        )}
      </Panel>

      <Panel>
        <h2 className="text-[0.9375rem] font-semibold text-ink">Good to know</h2>
        <ul className="mt-2 flex list-disc flex-col gap-1.5 pl-5 text-[0.8125rem] leading-relaxed text-muted">
          <li>
            <strong className="text-ink">Closing the window stops lending.</strong>{" "}
            Run the same command again whenever you want to lend again.
          </li>
          <li>
            A graphics card (NVIDIA) makes it much faster, but any computer
            helps.
          </li>
          <li>
            With Docker Desktop installed, each job runs sealed off from your
            files. Without it (Windows only), jobs run as an ordinary program —
            the installer explains the difference and asks you first.
          </li>
          <li>
            While your computer trains someone's job, their pictures pass
            through it. Only lend within a group you trust.
          </li>
        </ul>
        <p className="mt-3 text-xs text-muted">
          Want to train instead?{" "}
          <Link to="/datasets" className="underline underline-offset-2 hover:text-ink">
            Upload your pictures
          </Link>
        </p>
      </Panel>
    </div>
  );
}

function MyComputer({ node }: { node: NodeSummary }) {
  const gpus = node.hardware.gpus;
  const hardware =
    gpus.length === 0
      ? `${node.hardware.cpu_model}, no graphics card`
      : gpus.map((g) => `${g.name} (${formatBytes(g.vram_bytes)})`).join(", ");

  return (
    <li className="flex flex-wrap items-center gap-x-4 gap-y-1 py-3">
      <div className="flex min-w-0 flex-1 flex-col">
        <Link
          to={`/nodes/${node.id}`}
          className="font-data text-[0.8125rem] text-ink hover:underline"
        >
          {node.name}
        </Link>
        <span className="truncate text-xs text-muted">{hardware}</span>
      </div>
      <span className="text-xs text-muted">
        {node.last_heartbeat_at
          ? `seen ${formatRelativeTime(node.last_heartbeat_at)}`
          : "not connected yet"}
      </span>
      <StatusPill kind="node" status={node.status} />
      {node.status !== "ONLINE" && <RemoveNodeButton node={node} />}
    </li>
  );
}
