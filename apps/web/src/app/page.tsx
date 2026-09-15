"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { getToken } from "@/lib/api";

export default function Home() {
  const router = useRouter();
  useEffect(() => {
    // ICICI Breeze has no configurable callback path -- it redirects to
    // whatever bare URL was registered as the app's redirect, which for an
    // app registered with just the domain lands here, at /, carrying
    // ?apisession=... . Forwarding it onto /brokers rather than dropping it
    // on the floor is what makes "register the domain, not a path" work at
    // all; without this the query string was silently lost on every landing
    // here, and pasting the token required racing the redirect in dev tools.
    const params = new URLSearchParams(window.location.search);
    const apisession = params.get("apisession") ?? params.get("API_Session");
    if (apisession) {
      router.replace(`/brokers?apisession=${encodeURIComponent(apisession)}`);
      return;
    }
    router.replace(getToken() ? "/dashboard" : "/login");
  }, [router]);
  return null;
}
