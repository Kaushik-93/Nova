import { Suspense } from "react";

import { LoginForm } from "@/app/login/login-form";
import { Loading } from "@/components/states";

export default function LoginPage() {
  return <Suspense fallback={<Loading />}><LoginForm /></Suspense>;
}
