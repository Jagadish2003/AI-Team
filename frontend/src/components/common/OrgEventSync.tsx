import { useEffect } from 'react';
import { useAuthOptional } from '../../context/AuthContext';
import { connectOrgEventStream } from '../../lib/orgEventStream';

/**
 * Headless: keeps this browser in step with the rest of the org.
 *
 * Holds the org change stream open for the signed-in session. When another user
 * changes something, the stream pings and every mounted view refreshes what it
 * shows (DataCacheProvider revalidates observed cache keys; the hand-rolled
 * contexts refetch via useRevalidateOnFocus). Renders nothing.
 *
 * Must live inside AuthProvider (reads the token) and inside DataCacheProvider,
 * so the whole data layer is listening while it is mounted. The stream is
 * re-opened on token change (login / logout / accept-invite) and closed on
 * unmount; losing it degrades to the focus/interval fallbacks, never to stale
 * data.
 */
export default function OrgEventSync() {
  const auth = useAuthOptional();
  const token = auth?.token ?? null;

  useEffect(() => {
    if (!token) return;
    return connectOrgEventStream(token);
  }, [token]);

  return null;
}
