"use client";

import { useEffect, useState } from "react";
import { api, type Me } from "@/lib/api";
import { authConfigured, setAccessToken, supabase } from "@/lib/supabase";

// Signed-in state shared by the patient page and the staff dashboard.
// Demo mode (no Supabase keys): ready immediately, no sign-in, me.demo = true.
export function useAuth() {
  const [ready, setReady] = useState(false);
  const [signedIn, setSignedIn] = useState(false);
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!supabase) {
      api.me().then(setMe).catch((e: Error) => setError(e.message)).finally(() => setReady(true));
      return;
    }
    const apply = (token: string | null) => {
      setAccessToken(token);
      setSignedIn(!!token);
      if (token) {
        api.me().then(setMe).catch((e: Error) => setError(e.message)).finally(() => setReady(true));
      } else {
        setMe(null);
        setReady(true);
      }
    };
    supabase.auth.getSession().then(({ data }) => apply(data.session?.access_token ?? null));
    const { data: sub } = supabase.auth.onAuthStateChange((_event, session) => apply(session?.access_token ?? null));
    return () => sub.subscription.unsubscribe();
  }, []);

  return {
    ready,
    authConfigured,
    signedIn: authConfigured ? signedIn : true,
    me,
    error,
    signOut: () => supabase?.auth.signOut(),
  };
}
